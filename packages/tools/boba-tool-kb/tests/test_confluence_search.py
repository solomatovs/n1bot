"""Integration: `confluence_search` — online CQL по реальному Confluence.

Скипается, если не заполнены `[tool.kb.confluence]` (connection) или
`[test.kb].confluence_search_query`. Параметр `space` опционален —
если `[test.kb].confluence_search_space` задан, поиск ограничивается им.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from boba.tool.kb.confluence.config import ConfluenceConnectionConfig
from boba.tool.kb.confluence.search import confluence_search

if TYPE_CHECKING:
    from tests.conftest import KbIntegrationTestConfig

pytestmark = pytest.mark.integration


def test_confluence_search_returns_hits(
    confluence_cfg: ConfluenceConnectionConfig,
    test_cfg: KbIntegrationTestConfig,
) -> None:
    """`confluence_search` оборачивает query в CQL и возвращает ≥1 hit.

    Подразумевается, что test.kb.confluence_search_query написан так,
    что на реальном Confluence (см. `[tool.kb.confluence].base_url`)
    он матчит ≥1 страницу. На Apache cwiki/KAFKA подойдёт любое
    распространённое слово ("consumer", "broker", "topic").
    """
    if not test_cfg.confluence_search_query:
        pytest.skip("test.kb.confluence_search_query пусто")

    hits = confluence_search(
        query=test_cfg.confluence_search_query,
        limit=5,
        cfg=confluence_cfg,
        space=test_cfg.confluence_search_space or None,
    )

    assert isinstance(hits, list), f"expected list[dict], got {type(hits).__name__}"
    assert len(hits) >= 1, (
        f"query {test_cfg.confluence_search_query!r} вернул 0 hits "
        f"(space={test_cfg.confluence_search_space!r})"
    )
    for h in hits:
        # Контракт shape (общий с kb_search/fts_search → list[dict]).
        assert {"page_id", "title", "space_key", "url", "snippet"} <= h.keys(), h
        assert h["page_id"], "page_id обязан быть непустым"
        assert h["url"].startswith(
            confluence_cfg.base_url.rstrip("/"),
        ), f"url не из этого Confluence: {h['url']!r}"


def test_confluence_search_limit_respected(
    confluence_cfg: ConfluenceConnectionConfig,
    test_cfg: KbIntegrationTestConfig,
) -> None:
    """`limit` режет выдачу — len(hits) ≤ limit."""
    if not test_cfg.confluence_search_query:
        pytest.skip("test.kb.confluence_search_query пусто")

    hits = confluence_search(
        query=test_cfg.confluence_search_query,
        limit=2,
        cfg=confluence_cfg,
        space=test_cfg.confluence_search_space or None,
    )
    assert isinstance(hits, list)
    assert len(hits) <= 2
