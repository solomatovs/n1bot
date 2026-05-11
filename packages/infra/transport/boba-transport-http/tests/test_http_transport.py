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
    assert sid.to_wire() == "https://x.test/pages/viewpage.action?pageId=12345"
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
