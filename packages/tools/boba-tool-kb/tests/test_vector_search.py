"""Integration: `vector_search` — pure vector (cosine) по KB-коллекциям.

Использует те же фикстуры, что и `test_kb_search` (kb_cfg, search_cfg,
kb_knowledge_base). Параметры запроса — из `[test.kb].kb_search_query`
(общий с kb_search, так как target-коллекции те же).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from boba.tool.kb.core.config import KbConfig
from boba.tool.kb.core.kb import PostgresKnowledgeBase
from boba.tool.kb.core.search_config import SearchConfig
from boba.tool.kb.core.tools.vector_search import vector_search

if TYPE_CHECKING:
    from tests.conftest import KbIntegrationTestConfig

    from boba.tool.kb.core.embedding_config import EmbeddingConfig

pytestmark = pytest.mark.integration


def test_vector_search_real(
    kb_cfg: KbConfig,
    search_cfg: SearchConfig,
    embedding_cfg: EmbeddingConfig,
    kb_knowledge_base: PostgresKnowledgeBase,
    test_cfg: KbIntegrationTestConfig,
) -> None:
    """Реальный vector_search на pgvector-коллекциях оператора.

    Контракт: `list[dict]`, в каждом hit — `{id, distance, link, metadata,
    snippet}` (тот же shape, что и у kb_search — упрощает свитч между
    hybrid и pure-vector).
    """
    if not test_cfg.kb_search_query:
        pytest.skip("test.kb.kb_search_query пуст")

    hits = vector_search(
        query=test_cfg.kb_search_query,
        kb=kb_knowledge_base,
        cfg=kb_cfg,
        search_cfg=search_cfg,
        top_k=test_cfg.kb_search_top_k,
    )

    _emit("")
    _emit(f"collections:     {list(search_cfg.collections)}")
    _emit(f"embedding_model: {embedding_cfg.model}")
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
        # cosine-distance pgvector'а ∈ [0..2]; -0.0..2.001 разрешим
        # на округление double-precision.
        assert -0.001 <= h["distance"] <= 2.001, (
            f"unexpected cosine-distance: {h['distance']}"
        )
        assert isinstance(h["metadata"], dict)
        assert isinstance(h["snippet"], str)


def test_vector_search_top_k_ceiling(
    kb_cfg: KbConfig,
    search_cfg: SearchConfig,
    kb_knowledge_base: PostgresKnowledgeBase,
    test_cfg: KbIntegrationTestConfig,
) -> None:
    """`top_k > cfg.max_top_k` → RuntimeError."""
    query = test_cfg.kb_search_query or "test"
    with pytest.raises(RuntimeError, match="превышает max_top_k"):
        vector_search(
            query=query,
            kb=kb_knowledge_base,
            cfg=kb_cfg,
            search_cfg=search_cfg,
            top_k=kb_cfg.max_top_k + 1,
        )


def _emit(msg: str) -> None:
    print(msg)  # noqa: T201
