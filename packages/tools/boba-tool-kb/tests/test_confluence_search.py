"""Integration: `confluence_cql_search` — online CQL по реальному Confluence."""
# pyright: reportCallIssue=false

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from boba.tool.kb.confluence.tools.cql_search import (
    ConfluenceCqlSearchConfig,
    confluence_cql_search,
)

if TYPE_CHECKING:
    from tests.conftest import KbIntegrationTestConfig

pytestmark = pytest.mark.integration


def test_confluence_cql_search_returns_hits(
    confluence_cql_search_cfg: ConfluenceCqlSearchConfig,
    test_cfg: KbIntegrationTestConfig,
) -> None:
    """`confluence_cql_search` оборачивает query в CQL и возвращает ≥1 hit."""
    if not test_cfg.confluence_search_query:
        pytest.skip("test.kb.confluence_search_query пусто")

    hits = confluence_cql_search(
        cfg=confluence_cql_search_cfg,
        query=test_cfg.confluence_search_query,
        limit=5,
        space=test_cfg.confluence_search_space or None,
    )

    assert isinstance(hits, list)
    assert len(hits) >= 1
    for h in hits:
        assert {"page_id", "title", "space_key", "url", "snippet"} <= h.keys()
        assert h["page_id"]
        assert h["url"].startswith(
            confluence_cql_search_cfg.confluence.base_url.rstrip("/"),
        )


def test_confluence_cql_search_limit_respected(
    confluence_cql_search_cfg: ConfluenceCqlSearchConfig,
    test_cfg: KbIntegrationTestConfig,
) -> None:
    """`limit` режет выдачу — len(hits) ≤ limit."""
    if not test_cfg.confluence_search_query:
        pytest.skip("test.kb.confluence_search_query пусто")

    hits = confluence_cql_search(
        cfg=confluence_cql_search_cfg,
        query=test_cfg.confluence_search_query,
        limit=2,
        space=test_cfg.confluence_search_space or None,
    )
    assert isinstance(hits, list)
    assert len(hits) <= 2
