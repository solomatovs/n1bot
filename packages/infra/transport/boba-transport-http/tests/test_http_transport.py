"""HttpTransport: streaming HTTP → RawDocument c пробросом source_id + metadata."""

from __future__ import annotations

import httpx

from boba.indexing import Metadata, SourceId, TransportKeys
from boba.indexing.context import PipelineContext, PipelineId
from boba.transport.http import HttpKeys, HttpRequest, HttpTransport


def _ctx() -> PipelineContext:
    return PipelineContext(pipeline_id=PipelineId("t"))


def _patch(monkeypatch, handler):
    real_client = httpx.Client

    def mock_client(**kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(**kwargs)

    monkeypatch.setattr("boba.transport.http.transport.httpx.Client", mock_client)


def test_yields_raw_document_propagates_source_id_and_metadata(monkeypatch):
    def handler(_req):
        return httpx.Response(200, content=b"hello body", headers={"etag": '"v1"'})

    _patch(monkeypatch, handler)

    requests = [
        HttpRequest(
            url="https://x.test/rest/api/content/12345?expand=...",
            source_id=SourceId(
                "https://x.test/pages/viewpage.action?pageId=12345"
            ),
            metadata=Metadata.from_wire({"page_id": "12345"}),
        )
    ]
    seen = []
    for doc in HttpTransport().stream(_ctx(), iter(requests)):
        seen.append((doc.source_id, doc.metadata, doc.handle.read()))
    sid, md, payload = seen[0]
    # source_id берётся ИЗ Request'а, не из response.url
    assert sid == "https://x.test/pages/viewpage.action?pageId=12345"
    assert md.to_wire()["page_id"] == "12345"
    assert md.get(TransportKeys.ETAG) == "v1"
    assert md.get(HttpKeys.STATUS) == 200
    assert payload == b"hello body"


def test_handle_streams_in_chunks(monkeypatch):
    payload = b"a" * 5000

    def handler(_req):
        return httpx.Response(200, content=payload)

    _patch(monkeypatch, handler)

    chunks = []
    for doc in HttpTransport().stream(
        _ctx(),
        iter(
            [
                HttpRequest(
                    url="https://x.test/big",
                    source_id=SourceId("https://x.test/big"),
                )
            ]
        ),
    ):
        chunks.append(doc.handle.read(100))
        chunks.append(doc.handle.read(100))
        chunks.append(doc.handle.read())
    chunk1, chunk2, rest = chunks
    assert len(chunk1) == 100
    assert len(chunk2) == 100
    assert len(rest) == 5000 - 200
    assert chunk1 + chunk2 + rest == payload


def test_retry_recovers_after_5xx(monkeypatch):
    """5xx ретраится; запрос проходит, когда сервер перестаёт падать."""
    calls = {"n": 0}

    def handler(_req):
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(503, content=b"unavailable")
        return httpx.Response(200, content=b"ok")

    _patch(monkeypatch, handler)

    seen = list(
        HttpTransport(max_attempts=3, retry_backoff_sec=0).stream(
            _ctx(),
            iter(
                [HttpRequest(url="https://x.test/y", source_id=SourceId("y"))]
            ),
        )
    )
    assert calls["n"] == 3
    assert seen[0].handle.read() == b"ok"


def test_retry_exhausted_raises_last_5xx(monkeypatch):
    """Все попытки 5xx исчерпаны → пробрасывается HTTPStatusError."""
    calls = {"n": 0}

    def handler(_req):
        calls["n"] += 1
        return httpx.Response(500, content=b"boom")

    _patch(monkeypatch, handler)

    try:
        list(
            HttpTransport(max_attempts=2, retry_backoff_sec=0).stream(
                _ctx(),
                iter([HttpRequest(url="https://x.test/y", source_id=SourceId("y"))]),
            )
        )
    except httpx.HTTPStatusError as e:
        assert e.response.status_code == 500
    else:
        raise AssertionError("ожидался HTTPStatusError")
    assert calls["n"] == 2


def test_4xx_not_retried(monkeypatch):
    """4xx — клиентская ошибка, ретраев нет."""
    calls = {"n": 0}

    def handler(_req):
        calls["n"] += 1
        return httpx.Response(404, content=b"nope")

    _patch(monkeypatch, handler)

    try:
        list(
            HttpTransport(max_attempts=3, retry_backoff_sec=0).stream(
                _ctx(),
                iter([HttpRequest(url="https://x.test/y", source_id=SourceId("y"))]),
            )
        )
    except httpx.HTTPStatusError as e:
        assert e.response.status_code == 404
    else:
        raise AssertionError("ожидался HTTPStatusError")
    assert calls["n"] == 1


def test_auth_passed_to_client(monkeypatch):
    """HttpTransport пробрасывает `httpx.Auth` напрямую в `httpx.Client(auth=...)`."""
    seen_headers = {}

    def handler(req):
        seen_headers.update(req.headers)
        return httpx.Response(200, content=b"ok")

    _patch(monkeypatch, handler)

    list(
        HttpTransport().stream(
            _ctx(),
            iter(
                [
                    HttpRequest(
                        url="https://x.test/y",
                        source_id=SourceId("https://x.test/y"),
                        auth=httpx.BasicAuth(username="u", password="p"),
                    )
                ]
            ),
        )
    )
    # httpx.BasicAuth добавляет header через auth_flow поверх client'а.
    assert "authorization" in seen_headers
    assert seen_headers["authorization"].lower().startswith("basic ")
