"""PatAuth: применяет Authorization: Bearer <token> к исходящему запросу."""

from __future__ import annotations

import httpx

from boba.indexing import SourceId
from boba.indexing.context import PipelineContext, PipelineId
from boba.tool.confluence.auth import PatAuth
from boba.transport.http import HttpRequest, HttpTransport


def _ctx() -> PipelineContext:
    return PipelineContext(pipeline_id=PipelineId("t"))


def _patch(monkeypatch, handler):
    real_client = httpx.Client

    def mock_client(**kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(**kwargs)

    monkeypatch.setattr("boba.transport.http.transport.httpx.Client", mock_client)


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
                        source_id=SourceId("https://x.test/y"),
                        auth=PatAuth(token="secret-pat"),
                    )
                ]
            ),
        )
    )
    assert seen_headers.get("authorization") == "Bearer secret-pat"
