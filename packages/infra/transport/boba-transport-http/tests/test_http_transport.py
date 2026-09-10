"""HttpTransport: чистый HTTP-исполнитель -> HttpResponse (status/headers/stream)."""

from __future__ import annotations

import httpx
import pytest
from pydantic import SecretStr, ValidationError

from boba.transport.http import HttpRequest, HttpTransport
from boba.transport.http.profile import BasicAuth, HttpConnection, RetryStatuses

pytestmark = pytest.mark.anyio


@pytest.fixture(scope="module")
def anyio_backend() -> str:
    return "asyncio"


class _ChunkedStream(httpx.AsyncByteStream):
    """Ответ, приходящий несколькими чанками — как реальный сетевой поток."""

    def __init__(self, parts: list[bytes]) -> None:
        self._parts = parts

    async def __aiter__(self):
        for part in self._parts:
            yield part


def _patch(monkeypatch, handler):
    real_client = httpx.AsyncClient

    def mock_client(**kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(**kwargs)

    monkeypatch.setattr("boba.transport.http.transport.httpx.AsyncClient", mock_client)


async def test_returns_status_headers_and_body(monkeypatch):
    def handler(_req):
        return httpx.Response(200, content=b"hello body", headers={"etag": '"v1"'})

    _patch(monkeypatch, handler)

    async with (
        HttpTransport(HttpConnection(host="x.test", port=443)) as transport,
        transport.fetch(HttpRequest(url="https://x.test/doc")) as resp,
    ):
        # заголовки отдаются сырыми; обогащение/strip — забота потребителя
        if resp.status != 200:
            raise AssertionError("resp.status == 200")
        if resp.headers["etag"] != '"v1"':
            raise AssertionError('resp.headers["etag"] == \'"v1"\'')
        if await resp.stream.read() != b"hello body":
            raise AssertionError('await resp.stream.read() == b"hello body"')


async def test_body_arrives_as_a_stream(monkeypatch):
    """Чанки ответа доходят по одному: тело не собирается целиком."""
    parts = [b"alpha", b"beta", b"gamma"]

    def handler(_req):
        return httpx.Response(200, stream=_ChunkedStream(parts))

    _patch(monkeypatch, handler)

    seen: list[bytes] = []
    async with (
        HttpTransport(HttpConnection(host="x.test", port=443)) as transport,
        transport.fetch(HttpRequest(url="https://x.test/big")) as resp,
    ):
        async for chunk in resp.stream:
            seen.append(chunk)

    if seen != parts:
        raise AssertionError("seen == parts")


async def test_retry_recovers_after_5xx(monkeypatch):
    """5xx ретраится; запрос проходит, когда сервер перестаёт падать."""
    calls = {"n": 0}

    def handler(_req):
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(503, content=b"unavailable")
        return httpx.Response(200, content=b"ok")

    _patch(monkeypatch, handler)

    profile = HttpConnection(
        host="x.test", port=443, retry_attempts=3, retry_backoff_sec=0
    )
    async with (
        HttpTransport(profile) as transport,
        transport.fetch(HttpRequest(url="https://x.test/y")) as resp,
    ):
        body = await resp.stream.read()
    if calls["n"] != 3:
        raise AssertionError('calls["n"] == 3')
    if body != b"ok":
        raise AssertionError('body == b"ok"')


async def test_retry_exhausted_raises_last_5xx(monkeypatch):
    """Все попытки 5xx исчерпаны -> пробрасывается HTTPStatusError."""
    calls = {"n": 0}

    def handler(_req):
        calls["n"] += 1
        return httpx.Response(500, content=b"boom")

    _patch(monkeypatch, handler)

    transport = HttpTransport(
        HttpConnection(host="x.test", port=443, retry_attempts=2, retry_backoff_sec=0)
    )
    with pytest.raises(httpx.HTTPStatusError) as exc:
        async with transport.fetch(HttpRequest(url="https://x.test/y")):
            pass
    if exc.value.response.status_code != 500:
        raise AssertionError("exc.value.response.status_code == 500")
    if calls["n"] != 2:
        raise AssertionError('calls["n"] == 2')
    await transport.close()


async def test_4xx_not_retried(monkeypatch):
    """4xx — клиентская ошибка, ретраев нет."""
    calls = {"n": 0}

    def handler(_req):
        calls["n"] += 1
        return httpx.Response(404, content=b"nope")

    _patch(monkeypatch, handler)

    transport = HttpTransport(
        HttpConnection(host="x.test", port=443, retry_attempts=3, retry_backoff_sec=0)
    )
    with pytest.raises(httpx.HTTPStatusError) as exc:
        async with transport.fetch(HttpRequest(url="https://x.test/y")):
            pass
    if exc.value.response.status_code != 404:
        raise AssertionError("exc.value.response.status_code == 404")
    if calls["n"] != 1:
        raise AssertionError('calls["n"] == 1')
    await transport.close()


async def test_url_query_preserved_with_empty_params(monkeypatch):
    """Зашитый в url ?query не теряется при пустом params (httpx-gotcha)."""
    seen = {}

    def handler(req):
        seen["url"] = str(req.url)
        return httpx.Response(200, content=b"ok")

    _patch(monkeypatch, handler)

    req = HttpRequest(url="https://x.test/rest/api/content/1?expand=body.view")
    async with (
        HttpTransport(HttpConnection(host="x.test", port=443)) as transport,
        transport.fetch(req) as resp,
    ):
        await resp.stream.read()
    if seen["url"] != "https://x.test/rest/api/content/1?expand=body.view":
        raise AssertionError('seen["url"] == "https://x.test/rest/api/content/1?expan…')
    if not (transport.resolve_url(req).endswith("?expand=body.view")):
        raise AssertionError('transport.resolve_url(req).endswith("?expand=body.view")')


async def test_auth_from_profile_applied_to_client(monkeypatch):
    """HttpTransport применяет auth из профиля к httpx.AsyncClient."""
    seen_headers = {}

    def handler(req):
        seen_headers.update(req.headers)
        return httpx.Response(200, content=b"ok")

    _patch(monkeypatch, handler)

    profile = HttpConnection(
        host="x.test",
        port=443,
        auth=BasicAuth(method="basic", user="u", password=SecretStr("p")),
    )
    async with (
        HttpTransport(profile) as transport,
        transport.fetch(HttpRequest(url="https://x.test/y")) as resp,
    ):
        await resp.stream.read()
    # httpx.BasicAuth добавляет header через auth_flow поверх client'а.
    if "authorization" not in seen_headers:
        raise AssertionError('"authorization" in seen_headers')
    if not (seen_headers["authorization"].lower().startswith("basic ")):
        raise AssertionError('seen_headers["authorization"].lower().startswith("basic…')


async def test_status_listed_in_the_profile_is_retried(monkeypatch):
    """429 повторяется столько раз, сколько задано профилю для этого статуса."""
    calls = {"n": 0}

    def handler(_req):
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(429, content=b"slow down")
        return httpx.Response(200, content=b"ok")

    _patch(monkeypatch, handler)

    profile = HttpConnection(
        host="x.test",
        port=443,
        retry_attempts=1,
        retry_backoff_sec=0,
        retry_statuses=RetryStatuses({429: 3}),
    )
    async with (
        HttpTransport(profile) as transport,
        transport.fetch(HttpRequest(url="https://x.test/y")) as resp,
    ):
        body = await resp.stream.read()

    if calls["n"] != 3:
        raise AssertionError(f'429 is retried until it passes: {calls["n"]}')
    if body != b"ok":
        raise AssertionError('body == b"ok"')


async def test_status_outside_the_profile_is_not_retried(monkeypatch):
    """4xx, которого нет в таблице, остаётся клиентской ошибкой без повторов."""
    calls = {"n": 0}

    def handler(_req):
        calls["n"] += 1
        return httpx.Response(429, content=b"slow down")

    _patch(monkeypatch, handler)

    transport = HttpTransport(
        HttpConnection(
            host="x.test", port=443, retry_attempts=3, retry_backoff_sec=0
        )
    )
    with pytest.raises(httpx.HTTPStatusError) as exc:
        async with transport.fetch(HttpRequest(url="https://x.test/y")):
            pass

    if exc.value.response.status_code != 429:
        raise AssertionError("exc.value.response.status_code == 429")
    if calls["n"] != 1:
        raise AssertionError(f'a status without a rule is not retried: {calls["n"]}')

    await transport.close()


async def test_retry_after_header_sets_the_pause(monkeypatch):
    """Пауза перед повтором берётся из Retry-After, а не из backoff профиля."""
    slept: list[float] = []

    async def sleep(seconds: float) -> None:
        slept.append(seconds)

    monkeypatch.setattr("boba.transport.http.transport.asyncio.sleep", sleep)

    calls = {"n": 0}

    def handler(_req):
        calls["n"] += 1
        if calls["n"] < 2:
            return httpx.Response(429, headers={"retry-after": "7"})
        return httpx.Response(200, content=b"ok")

    _patch(monkeypatch, handler)

    profile = HttpConnection(
        host="x.test",
        port=443,
        retry_backoff_sec=0,
        retry_statuses=RetryStatuses({429: 2}),
        retry_after_max_sec=30,
    )
    async with (
        HttpTransport(profile) as transport,
        transport.fetch(HttpRequest(url="https://x.test/y")) as resp,
    ):
        await resp.stream.read()

    if slept != [7.0]:
        raise AssertionError(f"the pause comes from Retry-After: {slept}")


async def test_retry_after_is_capped_by_the_profile(monkeypatch):
    """Сервер может попросить час ожидания: ждём не дольше потолка профиля."""
    slept: list[float] = []

    async def sleep(seconds: float) -> None:
        slept.append(seconds)

    monkeypatch.setattr("boba.transport.http.transport.asyncio.sleep", sleep)

    calls = {"n": 0}

    def handler(_req):
        calls["n"] += 1
        if calls["n"] < 2:
            return httpx.Response(503, headers={"retry-after": "3600"})
        return httpx.Response(200, content=b"ok")

    _patch(monkeypatch, handler)

    profile = HttpConnection(
        host="x.test",
        port=443,
        retry_attempts=2,
        retry_backoff_sec=0,
        retry_after_max_sec=5,
    )
    async with (
        HttpTransport(profile) as transport,
        transport.fetch(HttpRequest(url="https://x.test/y")) as resp,
    ):
        await resp.stream.read()

    if slept != [5.0]:
        raise AssertionError(f"Retry-After is capped: {slept}")


class TestRetryStatusesValidation:
    """Таблица повторов разбирается на границе конфига, а не в транспорте."""

    def test_table_from_the_config_is_typed(self) -> None:
        profile = HttpConnection.model_validate(
            {"host": "x.test", "port": 443, "retry_statuses": {"429": "5"}}
        )

        if profile.retry_statuses.attempts_for(429) != 5:
            raise AssertionError(f"429 -> 5 attempts: {profile.retry_statuses.root}")
        if profile.retry_statuses.attempts_for(503) != 0:
            raise AssertionError("a status without a rule has no attempts")

    def test_status_outside_the_http_range_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="status code"):
            HttpConnection.model_validate(
                {"host": "x.test", "port": 443, "retry_statuses": {42: 2}}
            )

    def test_zero_attempts_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="at least one"):
            HttpConnection.model_validate(
                {"host": "x.test", "port": 443, "retry_statuses": {429: 0}}
            )
