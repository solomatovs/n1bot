"""Integration: `vector_search` — pure vector (cosine) поверх kb_chunks."""
# pyright: reportCallIssue=false

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from boba.tool.kb.core.tools.vector_search import VectorSearchConfig, vector_search

if TYPE_CHECKING:
    from tests.conftest import KbIntegrationTestConfig

pytestmark = pytest.mark.integration


def test_vector_search_real(
    vector_search_cfg: VectorSearchConfig,
    test_cfg: KbIntegrationTestConfig,
) -> None:
    """Реальный vector_search."""
    if not test_cfg.kb_search_query:
        pytest.skip("test.kb.kb_search_query пуст — задайте запрос для теста")

    hits = vector_search(
        cfg=vector_search_cfg,
        query=test_cfg.kb_search_query,
        top_k=test_cfg.kb_search_top_k,
    )

    assert isinstance(hits, list)
    for h in hits:
        assert {"id", "distance", "link", "metadata", "snippet"} <= h.keys()
        assert isinstance(h["distance"], float)


def test_vector_search_top_k_ceiling(
    vector_search_cfg: VectorSearchConfig,
    test_cfg: KbIntegrationTestConfig,
) -> None:
    """`top_k > cfg.max_top_k` → RuntimeError."""
    query = test_cfg.kb_search_query or "test"
    with pytest.raises(RuntimeError, match="превышает max_top_k"):
        vector_search(
            cfg=vector_search_cfg,
            query=query,
            top_k=vector_search_cfg.max_top_k + 1,
        )
