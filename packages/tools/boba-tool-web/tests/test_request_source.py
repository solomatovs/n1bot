"""WebUrlsRequestSource: stream → HttpRequest с правильным auth/source_id.

Запрещённый host бросает ValueError на первой же итерации — до того, как
кто-либо успеет открыть HTTP-сессию. Это ключевая инвариант whitelist'а.
"""

from __future__ import annotations

import httpx
import pytest

from boba.indexing import PipelineContext, PipelineId, SourceId
from boba.tool.web.connection import WebConnection
from boba.tool.web.request_source import WebUrlsRequestSource
from boba.transport.http import BearerAuth, HttpConnection
from boba.transport.http.auth import HttpxBearerAuth


def _ctx() -> PipelineContext:
    return PipelineContext(pipeline_id=PipelineId("test"))


def _conn() -> WebConnection:
    return WebConnection(
        profiles={
            "docs.python.org": HttpConnection(),
            "api.github.com": HttpConnection(
                auth=BearerAuth(method="bearer", token="tok"),
            ),
        },
    )


def test_stream_yields_one_request_per_url_with_source_id() -> None:
    src = WebUrlsRequestSource(
        urls=[
            "https://docs.python.org/3/library/asyncio.html",
            "https://api.github.com/repos/x/y",
        ],
        connection=_conn(),
    )
    reqs = list(src.stream(_ctx()))
    assert len(reqs) == 2
    assert reqs[0].source_id == SourceId(
        "https://docs.python.org/3/library/asyncio.html",
    )
    assert reqs[1].source_id == SourceId("https://api.github.com/repos/x/y")


def test_stream_picks_auth_by_host() -> None:
    src = WebUrlsRequestSource(
        urls=[
            "https://docs.python.org/3/",
            "https://api.github.com/x",
        ],
        connection=_conn(),
    )
    reqs = list(src.stream(_ctx()))
    assert reqs[0].auth is None
    assert isinstance(reqs[1].auth, HttpxBearerAuth)


def test_disallowed_host_raises_before_any_http() -> None:
    """ValueError должен прилететь из generator'а ещё до того,
    как Transport вообще получит хоть один HttpRequest."""
    src = WebUrlsRequestSource(
        urls=["https://evil.example.com/x"],
        connection=_conn(),
    )
    stream = src.stream(_ctx())
    with pytest.raises(ValueError, match="не в whitelist") as exc_info:
        next(iter(stream))
    assert "evil.example.com" in str(exc_info.value)


def test_partial_failure_stops_at_first_bad_host() -> None:
    """Один плохой URL посередине — стрим падает, прежние уже yield'ились."""
    src = WebUrlsRequestSource(
        urls=[
            "https://docs.python.org/3/",
            "https://evil.example.com/x",
            "https://api.github.com/y",
        ],
        connection=_conn(),
    )
    it = iter(src.stream(_ctx()))
    first = next(it)
    assert first.source_id == SourceId("https://docs.python.org/3/")
    with pytest.raises(ValueError, match="не в whitelist"):
        next(it)


def test_request_is_get_with_empty_headers() -> None:
    src = WebUrlsRequestSource(
        urls=["https://docs.python.org/3/"],
        connection=_conn(),
    )
    req = next(iter(src.stream(_ctx())))
    assert req.method == "GET"
    assert dict(req.headers) == {}


def test_works_with_httpx_basicauth_instance() -> None:
    """sanity: auth-инстанс — настоящий httpx.Auth (Transport его и ждёт)."""
    src = WebUrlsRequestSource(
        urls=["https://api.github.com/x"], connection=_conn(),
    )
    req = next(iter(src.stream(_ctx())))
    assert isinstance(req.auth, httpx.Auth)
