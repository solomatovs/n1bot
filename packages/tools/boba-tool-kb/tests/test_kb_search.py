"""Интеграционный тест `kb_search`: реальный postgres + pgvector + FTS + RRF.

Гибридный retrieval: vector top-K (cosine via `<=>`) + FTS top-K
(`plainto_tsquery`+`ts_rank_cd`), склейка через RRF.

Все общие объекты (pool, embedder, knowledge_base, cfg) приходят из
conftest.py через DI-фикстуры. Параметры запроса — из `[test.kb]`
(`KbIntegrationTestConfig`).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from boba.tool.kb.config import KbPluginConfig
from boba.tool.kb.kb import PostgresKnowledgeBase
from boba.tool.kb.kb_search import kb_search

if TYPE_CHECKING:
    from tests.conftest import KbIntegrationTestConfig

pytestmark = pytest.mark.integration


def test_kb_search_real(
    kb_cfg: KbPluginConfig,
    kb_knowledge_base: PostgresKnowledgeBase,
    test_cfg: KbIntegrationTestConfig,
) -> None:
    """Реальный kb_search на персистентном postgres оператора.

    Фикстуры скипают при отсутствии `[tool.kb]`/`[test.kb]`. Сам тест
    дополнительно скипается при пустом `test.kb.kb_search_query`.
    """
    if not test_cfg.kb_search_query:
        pytest.skip("test.kb.kb_search_query пуст — задайте запрос для теста")

    hits = kb_search(
        query=test_cfg.kb_search_query,
        kb=kb_knowledge_base,
        cfg=kb_cfg,
        top_k=test_cfg.kb_search_top_k,
    )

    _emit("")
    _emit(f"dsn:             {_mask_dsn(kb_cfg.dsn)}")
    _emit(f"collection:      {kb_cfg.ingest_collection}")
    _emit(f"embedding_model: {kb_cfg.embedding_model}")
    _emit(f"fts_language:    {kb_cfg.fts_language}")
    _emit(f"rrf_k:           {kb_cfg.rrf_k}")
    _emit(f"rrf_pool:        {kb_cfg.rrf_pool}")
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
    kb_cfg: KbPluginConfig,
    kb_knowledge_base: PostgresKnowledgeBase,
    test_cfg: KbIntegrationTestConfig,
) -> None:
    """`top_k > cfg.max_top_k` → RuntimeError. Защита от перегрузки KB."""
    query = test_cfg.kb_search_query or "test"
    with pytest.raises(RuntimeError, match="превышает max_top_k"):
        kb_search(
            query=query,
            kb=kb_knowledge_base,
            cfg=kb_cfg,
            top_k=kb_cfg.max_top_k + 1,
        )


def _mask_dsn(dsn: str) -> str:
    if "@" not in dsn or "://" not in dsn:
        return dsn
    scheme, rest = dsn.split("://", 1)
    creds, host = rest.split("@", 1)
    if ":" in creds:
        user, _ = creds.split(":", 1)
        return f"{scheme}://{user}:***@{host}"
    return dsn


def _emit(msg: str) -> None:
    print(msg)  # noqa: T201
