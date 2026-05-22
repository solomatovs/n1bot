"""Integration: `fts_search` — pure FTS по pre-настроенной таблице оператора.

Скипается, если не заполнены `[tool.kb.fts].index` (whitelist-таблица),
`[tool.kb.postgres]` (Pool) или `[test.kb].fts_query` (что искать).

Поскольку таблица — внешняя (оператор разворачивает её сам), у нас нет
готового публичного бэкенда для теста (в отличие от cwiki для Confluence).
Тест проверяет контракт shape возвращаемых hits на любой реальной
таблице, которую оператор настроил.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from boba.tool.kb.fts.config import FtsConfig
from boba.tool.kb.fts.db import PgFtsKnowledgeBase
from boba.tool.kb.fts.tools.fts_search import fts_search

if TYPE_CHECKING:
    from tests.conftest import KbIntegrationTestConfig

pytestmark = pytest.mark.integration


def test_fts_search_returns_hits_shape(
    fts_cfg: FtsConfig,
    pg_fts_kb: PgFtsKnowledgeBase,
    test_cfg: KbIntegrationTestConfig,
) -> None:
    """`fts_search` возвращает `list[dict]` со стандартным контрактом.

    Не проверяет конкретное содержимое (зависит от таблицы оператора),
    только: тип результата = list, для каждого hit обязательны поля
    {id, score, metadata, snippet}, score — float, snippet — str.
    Если query попадает в 0 строк — это валидный результат (пустой list).
    """
    if not test_cfg.fts_query:
        pytest.skip("test.kb.fts_query пусто")

    hits = fts_search(
        query=test_cfg.fts_query,
        kb=pg_fts_kb,
        cfg=fts_cfg,
        top_k=5,
    )

    assert isinstance(hits, list), f"expected list[dict], got {type(hits).__name__}"
    for h in hits:
        assert {"id", "score", "metadata", "snippet"} <= h.keys(), h
        assert isinstance(h["score"], float)
        assert isinstance(h["metadata"], dict)
        assert isinstance(h["snippet"], str)


def test_fts_search_top_k_ceiling(
    fts_cfg: FtsConfig,
    pg_fts_kb: PgFtsKnowledgeBase,
    test_cfg: KbIntegrationTestConfig,
) -> None:
    """`top_k > cfg.max_top_k` → RuntimeError."""
    query = test_cfg.fts_query or "test"
    with pytest.raises(RuntimeError, match="превышает max_top_k"):
        fts_search(
            query=query,
            kb=pg_fts_kb,
            cfg=fts_cfg,
            top_k=fts_cfg.max_top_k + 1,
        )
