"""Integration: `kb_search` — hybrid (vector + FTS + RRF) поверх kb_chunks."""
# pyright: reportCallIssue=false

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from boba.tool.kb.core.tools.kb_search import KbSearchConfig, kb_search

if TYPE_CHECKING:
    from tests.conftest import KbIntegrationTestConfig

pytestmark = pytest.mark.integration


def test_kb_search_real(
    kb_search_cfg: KbSearchConfig,
    test_cfg: KbIntegrationTestConfig,
) -> None:
    """Реальный kb_search."""
    if not test_cfg.kb_search_query:
        pytest.skip("test.kb.kb_search_query пуст — задайте запрос для теста")

    hits = kb_search(
        cfg=kb_search_cfg,
        query=test_cfg.kb_search_query,
        top_k=test_cfg.kb_search_top_k,
    )

    _emit("")
    _emit(f"collections:     {list(kb_search_cfg.collections)}")
    _emit(f"embedding_model: {kb_search_cfg.knowledge_base.embedding.model}")
    _emit(f"fts_language:    {kb_search_cfg.knowledge_base.fts_language}")
    _emit(f"rrf_k:           {kb_search_cfg.knowledge_base.rrf_k}")
    _emit(f"rrf_pool:        {kb_search_cfg.knowledge_base.rrf_pool}")
    _emit(f"query:           {test_cfg.kb_search_query!r}")
    _emit(f"top_k:           {test_cfg.kb_search_top_k}")
    _emit(f"hits:            {len(hits)}")
    for i, h in enumerate(hits):
        _emit(
            f"  [{i}] dist={h['distance']:.4f}  id={h['id']}  link={h['link']}",
        )
        _emit(f"       snippet: {h['snippet']}")

    assert isinstance(hits, list)
    for h in hits:
        assert {"id", "distance", "link", "metadata", "snippet"} <= h.keys()
        assert isinstance(h["distance"], float)
        assert isinstance(h["metadata"], dict)
        assert isinstance(h["snippet"], str)


def test_kb_search_top_k_ceiling(
    kb_search_cfg: KbSearchConfig,
    test_cfg: KbIntegrationTestConfig,
) -> None:
    """`top_k > cfg.max_top_k` → RuntimeError. Защита от перегрузки KB."""
    query = test_cfg.kb_search_query or "test"
    with pytest.raises(RuntimeError, match="превышает max_top_k"):
        kb_search(
            cfg=kb_search_cfg,
            query=query,
            top_k=kb_search_cfg.max_top_k + 1,
        )


def _emit(msg: str) -> None:
    print(msg)  # noqa: T201
