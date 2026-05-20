"""PatAuth: применяет Authorization: Bearer <token> к исходящему запросу."""

from __future__ import annotations

from collections.abc import Callable

import httpx

from boba.indexing import SourceId
from boba.indexing.context import PipelineContext
from boba.tool.confluence.auth import PatAuth
from boba.transport.http import HttpRequest, HttpTransport

_HTTPX_TARGET = "boba.transport.http.transport.httpx.Client"

_PatchHttpx = Callable[[str, Callable[[httpx.Request], httpx.Response]], None]


def test_pat_auth_applies_bearer(
    pipeline_ctx: PipelineContext,
    patch_httpx: _PatchHttpx,
):
    seen_headers = {}

    def handler(req):
        seen_headers.update(req.headers)
        return httpx.Response(200, content=b"ok")

    patch_httpx(_HTTPX_TARGET, handler)

    list(
        HttpTransport().stream(
            pipeline_ctx,
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
