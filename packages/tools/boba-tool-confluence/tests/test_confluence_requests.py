"""ConfluenceSpace/Pages/Cql RequestSources: REST URL'ы + viewpage source_id."""

from __future__ import annotations

import json
from collections.abc import Callable

import httpx
import pytest

from boba.indexing import PipelineContext
from boba.tool.confluence.auth import PatAuth
from boba.tool.confluence.keys import ConfluenceKeys
from boba.tool.confluence.request_sources import (
    ConfluenceCqlRequestSource,
    ConfluencePagesRequestSource,
    ConfluenceSpaceRequestSource,
)

_HTTPX_TARGET = "boba.tool.confluence.request_sources._common.httpx.Client"

_PatchHttpx = Callable[[str, Callable[[httpx.Request], httpx.Response]], None]


def test_pages_source_sets_viewpage_source_id_and_rest_url(
    pipeline_ctx: PipelineContext,
):
    """RequestSource заполняет:
    - `url` = REST endpoint (для Transport),
    - `source_id` = stable viewpage URL (для identity).
    """
    src = ConfluencePagesRequestSource(
        base_url="https://confl.test",
        auth=PatAuth("t"),
        page_ids=["111", "222"],
    )
    requests = list(src.stream(pipeline_ctx))
    # url = REST endpoint с expand
    assert all("rest/api/content/" in r.url for r in requests)
    assert all("expand=body.export_view" in r.url for r in requests)
    # source_id = stable viewpage URL — НЕ равен r.url
    assert (
        requests[0].source_id
        == "https://confl.test/pages/viewpage.action?pageId=111"
    )
    assert (
        requests[1].source_id
        == "https://confl.test/pages/viewpage.action?pageId=222"
    )
    assert all(r.source_id != r.url for r in requests)
    # metadata содержит structured-данные для kb_search
    assert all(r.metadata.get(ConfluenceKeys.PAGE_ID) for r in requests)
    assert all(r.metadata.get(ConfluenceKeys.HOST) == "confl.test" for r in requests)
    assert all(r.auth is not None for r in requests)


def test_pages_list_source_ids_returns_viewpage_urls(
    pipeline_ctx: PipelineContext,
):
    src = ConfluencePagesRequestSource(
        base_url="https://confl.test",
        auth=None,
        page_ids=["1", "2"],
    )
    ids = list(src.list_source_ids(pipeline_ctx))
    assert ids == [
        "https://confl.test/pages/viewpage.action?pageId=1",
        "https://confl.test/pages/viewpage.action?pageId=2",
    ]


def test_space_source_paginates_via_discovery(
    pipeline_ctx: PipelineContext,
    patch_httpx: _PatchHttpx,
):
    pages: dict[str, dict] = {
        "/rest/api/space/DOCS/content?type=page&limit=50&start=0": {
            "results": [{"id": "100"}, {"id": "200"}],
            "_links": {"next": "/rest/api/space/DOCS/content?start=50"},
        },
        "/rest/api/space/DOCS/content?start=50": {
            "results": [{"id": "300"}],
            "_links": {},
        },
    }

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path + ("?" + req.url.query.decode() if req.url.query else "")
        body = pages[path]
        return httpx.Response(200, content=json.dumps(body))

    patch_httpx(_HTTPX_TARGET, handler)

    src = ConfluenceSpaceRequestSource(
        base_url="https://confl.test",
        auth=PatAuth("t"),
        space_key="DOCS",
    )
    ids = list(src.list_source_ids(pipeline_ctx))
    assert ids == [
        "https://confl.test/pages/viewpage.action?pageId=100",
        "https://confl.test/pages/viewpage.action?pageId=200",
        "https://confl.test/pages/viewpage.action?pageId=300",
    ]


def test_space_source_stream_emits_one_request_per_page(
    pipeline_ctx: PipelineContext,
    patch_httpx: _PatchHttpx,
):
    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=json.dumps({"results": [{"id": "777"}]}))

    patch_httpx(_HTTPX_TARGET, handler)

    src = ConfluenceSpaceRequestSource(
        base_url="https://confl.test",
        auth=PatAuth("t"),
        space_key="DOCS",
    )
    requests = list(src.stream(pipeline_ctx))
    assert len(requests) == 1
    r = requests[0]
    assert "rest/api/content/777" in r.url
    assert (
        r.source_id
        == "https://confl.test/pages/viewpage.action?pageId=777"
    )


def test_cql_source_uses_search_endpoint(
    pipeline_ctx: PipelineContext,
    patch_httpx: _PatchHttpx,
):
    captured = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["path"] = req.url.path + "?" + req.url.query.decode()
        return httpx.Response(200, content=json.dumps({"results": [{"id": "42"}]}))

    patch_httpx(_HTTPX_TARGET, handler)

    src = ConfluenceCqlRequestSource(
        base_url="https://confl.test",
        auth=None,
        cql="space = DOCS AND label = api",
    )
    ids = list(src.list_source_ids(pipeline_ctx))
    assert ids == ["https://confl.test/pages/viewpage.action?pageId=42"]
    assert "/rest/api/content/search" in captured["path"]
    assert "space%20%3D%20DOCS" in captured["path"]


def test_pages_request_url_includes_expand_for_body_format(
    pipeline_ctx: PipelineContext,
):
    src = ConfluencePagesRequestSource(
        base_url="https://confl.test",
        auth=None,
        page_ids=["1"],
        body_format="storage",
    )
    req = next(iter(src.stream(pipeline_ctx)))
    assert "expand=body.storage" in req.url
    # source_id не зависит от body_format — стабилен
    assert (
        req.source_id
        == "https://confl.test/pages/viewpage.action?pageId=1"
    )


def test_request_url_strips_trailing_slash_in_base_url(
    pipeline_ctx: PipelineContext,
):
    src = ConfluencePagesRequestSource(
        base_url="https://confl.test/",
        auth=None,
        page_ids=["1"],
    )
    req = next(iter(src.stream(pipeline_ctx)))
    assert "//rest/api" not in req.url
    assert "//pages/viewpage" not in req.source_id


def test_metadata_carries_page_id_and_host(
    pipeline_ctx: PipelineContext,
):
    """structured-данные для kb_search-фильтра."""
    src = ConfluencePagesRequestSource(
        base_url="https://confl.x.com",
        auth=None,
        page_ids=["12345"],
    )
    req = next(iter(src.stream(pipeline_ctx)))
    assert req.metadata.get(ConfluenceKeys.PAGE_ID) == "12345"
    assert req.metadata.get(ConfluenceKeys.HOST) == "confl.x.com"


def test_no_auth_passes_none(
    pipeline_ctx: PipelineContext,
    patch_httpx: _PatchHttpx,
):
    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=json.dumps({"results": []}))

    patch_httpx(_HTTPX_TARGET, handler)

    src = ConfluenceSpaceRequestSource(
        base_url="https://confl.test",
        auth=None,
        space_key="X",
    )
    requests = list(src.stream(pipeline_ctx))
    assert requests == []


@pytest.mark.parametrize(
    ("base_url", "expected_host"),
    [
        ("https://confl.test", "confl.test"),
        ("https://confl.test/wiki/", "confl.test"),
        ("http://localhost:8090", "localhost:8090"),
    ],
)
def test_host_extraction(
    pipeline_ctx: PipelineContext,
    base_url: str,
    expected_host: str,
):
    src = ConfluencePagesRequestSource(
        base_url=base_url,
        auth=None,
        page_ids=["1"],
    )
    req = next(iter(src.stream(pipeline_ctx)))
    assert req.metadata.get(ConfluenceKeys.HOST) == expected_host
