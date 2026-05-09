"""HttpTransport: streaming HTTP → RawDocument c пробросом source_id + metadata."""

from __future__ import annotations

import httpx
import pytest

from boba.http_transport import HttpRequest, HttpTransport, PatAuth
from boba.processing import PipelineContext, PipelineId


def _ctx() -> PipelineContext:
    return PipelineContext(pipeline_id=PipelineId("t"), collection="c")


def _patch(monkeypatch, handler):
    real_client = httpx.Client

    def mock_client(**kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(**kwargs)

    monkeypatch.setattr("boba.http_transport.transport.httpx.Client", mock_client)


def test_yields_raw_document_propagates_source_id_and_metadata(monkeypatch):
    def handler(_req):
        return httpx.Response(200, content=b"hello body", headers={"etag": '"v1"'})

    _patch(monkeypatch, handler)

    requests = [
        HttpRequest(
            url="https://x.test/rest/api/content/12345?expand=...",
            source_id="https://x.test/pages/viewpage.action?pageId=12345",
            metadata={"page_id": "12345"},
        )
    ]
    seen = []
    for doc in HttpTransport().stream(_ctx(), iter(requests)):
        seen.append((doc.source_id, dict(doc.metadata), doc.handle.read()))
    sid, md, payload = seen[0]
    # source_id берётся ИЗ Request'а, не из response.url
    assert sid == "https://x.test/pages/viewpage.action?pageId=12345"
    assert md["page_id"] == "12345"
    assert md["etag"] == "v1"
    assert md["status"] == "200"
    assert payload == b"hello body"


def test_handle_streams_in_chunks(monkeypatch):
    payload = b"a" * 5000

    def handler(_req):
        return httpx.Response(200, content=payload)

    _patch(monkeypatch, handler)

    chunks = []
    for doc in HttpTransport().stream(
        _ctx(),
        iter([HttpRequest(url="https://x.test/big", source_id="https://x.test/big")]),
    ):
        chunks.append(doc.handle.read(100))
        chunks.append(doc.handle.read(100))
        chunks.append(doc.handle.read())
    chunk1, chunk2, rest = chunks
    assert len(chunk1) == 100
    assert len(chunk2) == 100
    assert len(rest) == 5000 - 200
    assert chunk1 + chunk2 + rest == payload


def test_pat_auth_applies_bearer(monkeypatch):
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
                        source_id="https://x.test/y",
                        auth=PatAuth(token="secret-pat"),
                    )
                ]
            ),
        )
    )
    assert seen_headers.get("authorization") == "Bearer secret-pat"


def test_missing_source_id_raises(monkeypatch):
    """Transport — исполнитель: identity обязан быть установлен RequestSource'ом.
    Пустой source_id это ошибка контракта, не fallback.
    """

    def handler(_req):
        return httpx.Response(200, content=b"")

    _patch(monkeypatch, handler)

    with pytest.raises(ValueError, match="source_id"):
        list(
            HttpTransport().stream(
                _ctx(), iter([HttpRequest(url="https://x.test/raw")])
            )
        )
