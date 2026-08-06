"""HttpTransport: чистый HTTP-исполнитель -> HttpResponse (status/headers/stream)."""

from __future__ import annotations

import httpx
import pytest
from pydantic import SecretStr

from boba.transport.http import BasicAuth, HttpProfile, HttpRequest, HttpTransport

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
        HttpTransport(HttpProfile()) as transport,
        transport.fetch(HttpRequest(url="https://x.test/doc")) as resp,
    ):
        # заголовки отдаются сырыми; обогащение/strip — забота потребителя
        assert resp.status == 200
        assert resp.headers["etag"] == '"v1"'
        assert await resp.stream.read() == b"hello body"


async def test_body_arrives_as_a_stream(monkeypatch):
    """Чанки ответа доходят по одному: тело не собирается целиком."""
    parts = [b"alpha", b"beta", b"gamma"]

    def handler(_req):
        return httpx.Response(200, stream=_ChunkedStream(parts))

    _patch(monkeypatch, handler)

    seen: list[bytes] = []
    async with (
        HttpTransport(HttpProfile()) as transport,
        transport.fetch(HttpRequest(url="https://x.test/big")) as resp,
    ):
        async for chunk in resp.stream:
            seen.append(chunk)

    assert seen == parts


async def test_retry_recovers_after_5xx(monkeypatch):
    """5xx ретраится; запрос проходит, когда сервер перестаёт падать."""
    calls = {"n": 0}

    def handler(_req):
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(503, content=b"unavailable")
        return httpx.Response(200, content=b"ok")

    _patch(monkeypatch, handler)

    profile = HttpProfile(retry_attempts=3, retry_backoff_sec=0)
    async with (
        HttpTransport(profile) as transport,
        transport.fetch(HttpRequest(url="https://x.test/y")) as resp,
    ):
        body = await resp.stream.read()
    assert calls["n"] == 3
    assert body == b"ok"


async def test_retry_exhausted_raises_last_5xx(monkeypatch):
    """Все попытки 5xx исчерпаны -> пробрасывается HTTPStatusError."""
    calls = {"n": 0}

    def handler(_req):
        calls["n"] += 1
        return httpx.Response(500, content=b"boom")

    _patch(monkeypatch, handler)

    transport = HttpTransport(HttpProfile(retry_attempts=2, retry_backoff_sec=0))
    with pytest.raises(httpx.HTTPStatusError) as exc:
        async with transport.fetch(HttpRequest(url="https://x.test/y")):
            pass
    assert exc.value.response.status_code == 500
    assert calls["n"] == 2
    await transport.close()


async def test_4xx_not_retried(monkeypatch):
    """4xx — клиентская ошибка, ретраев нет."""
    calls = {"n": 0}

    def handler(_req):
        calls["n"] += 1
        return httpx.Response(404, content=b"nope")

    _patch(monkeypatch, handler)

    transport = HttpTransport(HttpProfile(retry_attempts=3, retry_backoff_sec=0))
    with pytest.raises(httpx.HTTPStatusError) as exc:
        async with transport.fetch(HttpRequest(url="https://x.test/y")):
            pass
    assert exc.value.response.status_code == 404
    assert calls["n"] == 1
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
        HttpTransport(HttpProfile()) as transport,
        transport.fetch(req) as resp,
    ):
        await resp.stream.read()
    assert seen["url"] == "https://x.test/rest/api/content/1?expand=body.view"
    assert transport.resolve_url(req).endswith("?expand=body.view")


async def test_auth_from_profile_applied_to_client(monkeypatch):
    """HttpTransport применяет auth из профиля к httpx.AsyncClient."""
    seen_headers = {}

    def handler(req):
        seen_headers.update(req.headers)
        return httpx.Response(200, content=b"ok")

    _patch(monkeypatch, handler)

    profile = HttpProfile(
        auth=BasicAuth(method="basic", user="u", password=SecretStr("p"))
    )
    async with (
        HttpTransport(profile) as transport,
        transport.fetch(HttpRequest(url="https://x.test/y")) as resp,
    ):
        await resp.stream.read()
    # httpx.BasicAuth добавляет header через auth_flow поверх client'а.
    assert "authorization" in seen_headers
    assert seen_headers["authorization"].lower().startswith("basic ")
