"""ConfluenceSpace/Pages/Cql RequestSources: REST URL'ы + viewpage source_id.

Часть тестов — URL-shape (без HTTP): просто конструируют `RequestSource`
и проверяют сгенерированные `HttpRequest`'ы. Они не требуют backend'а;
параметры — фиксированные тестовые значения, отдельный config не нужен.

Часть тестов — integration: реальная Confluence-discovery через
`patch_httpx`'у больше нет места. Эти тесты используют параметры
из `[test.kb]` (`confluence_space_key` / `confluence_cql`) и фикстуру
`confluence_auth` (реальный `PatAuth`/`BasicAuth`) из conftest.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import httpx
import pytest

from boba.indexing import PipelineContext
from boba.tool.kb.confluence.auth import PatAuth
from boba.tool.kb.confluence.config import ConfluenceConnectionConfig
from boba.tool.kb.confluence.keys import ConfluenceKeys
from boba.tool.kb.confluence.request_sources import (
    ConfluenceCqlRequestSource,
    ConfluencePagesRequestSource,
    ConfluenceSpaceRequestSource,
)

if TYPE_CHECKING:
    from tests.conftest import KbIntegrationTestConfig

pytestmark = pytest.mark.integration


# --------------------------------------------------------------------------- #
# URL-shape тесты (без HTTP-походов, без моков; на реальных RequestSource'ах).
# --------------------------------------------------------------------------- #


def test_pages_source_sets_viewpage_source_id_and_rest_url(
    pipeline_ctx: PipelineContext,
) -> None:
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
    assert all("rest/api/content/" in r.url for r in requests)
    assert all("expand=body.export_view" in r.url for r in requests)
    assert (
        requests[0].source_id
        == "https://confl.test/pages/viewpage.action?pageId=111"
    )
    assert (
        requests[1].source_id
        == "https://confl.test/pages/viewpage.action?pageId=222"
    )
    assert all(r.source_id != r.url for r in requests)
    assert all(r.metadata.get(ConfluenceKeys.PAGE_ID) for r in requests)
    assert all(r.metadata.get(ConfluenceKeys.HOST) == "confl.test" for r in requests)
    assert all(r.auth is not None for r in requests)


def test_pages_list_source_ids_returns_viewpage_urls(
    pipeline_ctx: PipelineContext,
) -> None:
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


def test_pages_request_url_includes_expand_for_body_format(
    pipeline_ctx: PipelineContext,
) -> None:
    src = ConfluencePagesRequestSource(
        base_url="https://confl.test",
        auth=None,
        page_ids=["1"],
        body_format="storage",
    )
    req = next(iter(src.stream(pipeline_ctx)))
    assert "expand=body.storage" in req.url
    # source_id не зависит от body_format
    assert (
        req.source_id
        == "https://confl.test/pages/viewpage.action?pageId=1"
    )


def test_request_url_strips_trailing_slash_in_base_url(
    pipeline_ctx: PipelineContext,
) -> None:
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
) -> None:
    """structured-данные для kb_search-фильтра."""
    src = ConfluencePagesRequestSource(
        base_url="https://confl.x.com",
        auth=None,
        page_ids=["12345"],
    )
    req = next(iter(src.stream(pipeline_ctx)))
    assert req.metadata.get(ConfluenceKeys.PAGE_ID) == "12345"
    assert req.metadata.get(ConfluenceKeys.HOST) == "confl.x.com"


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
) -> None:
    src = ConfluencePagesRequestSource(
        base_url=base_url,
        auth=None,
        page_ids=["1"],
    )
    req = next(iter(src.stream(pipeline_ctx)))
    assert req.metadata.get(ConfluenceKeys.HOST) == expected_host


# --------------------------------------------------------------------------- #
# Integration: реальная Confluence discovery (space-pagination + CQL-search).
# --------------------------------------------------------------------------- #


def test_space_source_returns_pages_from_real_space(
    pipeline_ctx: PipelineContext,
    confluence_cfg: ConfluenceConnectionConfig,
    confluence_auth: httpx.Auth | None,
    test_cfg: KbIntegrationTestConfig,
) -> None:
    """Реальная discovery: SpaceRequestSource перечисляет страницы space-а.

    Если в space ≥1 страница, `list_source_ids` отдаёт ≥1 viewpage-URL, и
    `stream` эмитит соответствующее количество `HttpRequest`'ов.
    """
    if not test_cfg.confluence_space_key:
        pytest.skip("test.kb.confluence_space_key пусто")

    src = ConfluenceSpaceRequestSource(
        base_url=confluence_cfg.base_url,
        auth=confluence_auth,
        space_key=test_cfg.confluence_space_key,
        body_format=confluence_cfg.body_format,
        timeout_sec=confluence_cfg.timeout_sec,
    )
    ids = list(src.list_source_ids(pipeline_ctx))
    assert ids, f"space {test_cfg.confluence_space_key!r} вернул 0 страниц"
    assert all(
        i.startswith(f"{confluence_cfg.base_url.rstrip('/')}/pages/viewpage.action?pageId=")
        for i in ids
    )

    # stream() должен дать тот же набор страниц (через тот же discovery)
    requests = list(src.stream(pipeline_ctx))
    assert len(requests) == len(ids)
    for r in requests:
        assert "rest/api/content/" in r.url
        assert r.source_id in ids


def test_cql_source_returns_pages_for_real_cql(
    pipeline_ctx: PipelineContext,
    confluence_cfg: ConfluenceConnectionConfig,
    confluence_auth: httpx.Auth | None,
    test_cfg: KbIntegrationTestConfig,
) -> None:
    """Реальный CQL-search: возвращает ≥1 страницу + правильные HttpRequest'ы.

    Подразумевается, что `test.kb.confluence_cql` написан так, что он
    действительно матчит ≥1 страницу (иначе тест провалится — это
    feature, чтобы кривой CQL не был тихим no-op).
    """
    if not test_cfg.confluence_cql:
        pytest.skip("test.kb.confluence_cql пусто")

    src = ConfluenceCqlRequestSource(
        base_url=confluence_cfg.base_url,
        auth=confluence_auth,
        cql=test_cfg.confluence_cql,
        body_format=confluence_cfg.body_format,
        timeout_sec=confluence_cfg.timeout_sec,
    )
    ids = list(src.list_source_ids(pipeline_ctx))
    assert ids, f"cql {test_cfg.confluence_cql!r} вернул 0 страниц"

    requests = list(src.stream(pipeline_ctx))
    assert len(requests) == len(ids)
    for r in requests:
        assert "rest/api/content/" in r.url
        assert r.source_id in ids
