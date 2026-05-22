"""Integration: `fts_search` — pure FTS по pre-настроенной таблице оператора."""
# pyright: reportCallIssue=false

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from boba.tool.kb.fts.tools.fts_search import FtsSearchConfig, fts_search

if TYPE_CHECKING:
    from tests.conftest import KbIntegrationTestConfig

pytestmark = pytest.mark.integration


def test_fts_search_returns_hits_shape(
    fts_search_cfg: FtsSearchConfig,
    test_cfg: KbIntegrationTestConfig,
) -> None:
    """`fts_search` возвращает `list[dict]` со стандартным контрактом."""
    if not test_cfg.fts_query:
        pytest.skip("test.kb.fts_query пусто")

    hits = fts_search(
        cfg=fts_search_cfg,
        query=test_cfg.fts_query,
        top_k=5,
    )

    assert isinstance(hits, list)
    for h in hits:
        assert {"id", "score", "metadata", "snippet"} <= h.keys()
        assert isinstance(h["score"], float)
        assert isinstance(h["metadata"], dict)
        assert isinstance(h["snippet"], str)


def test_fts_search_top_k_ceiling(
    fts_search_cfg: FtsSearchConfig,
    test_cfg: KbIntegrationTestConfig,
) -> None:
    """`top_k > cfg.max_top_k` → RuntimeError."""
    query = test_cfg.fts_query or "test"
    with pytest.raises(RuntimeError, match="превышает max_top_k"):
        fts_search(
            cfg=fts_search_cfg,
            query=query,
            top_k=fts_search_cfg.max_top_k + 1,
        )
