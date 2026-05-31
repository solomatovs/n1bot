"""Локальные DTO KB-плагина (response-форматы для tools)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

__all__ = ["SearchHit"]


@dataclass(frozen=True)
class SearchHit:
    """Один результат kb_search_* (vector/fts).

    `distance` — cosine-distance (`vector_search`) либо отрицательный
    `ts_rank_cd`-скор (`fts_search`); в обоих случаях семантика
    «меньше = ближе/релевантнее».

    `metadata` — сырой набор ключей чанка (все слои pipeline'а), `tags` —
    колонка `kb_chunks.tags`. Сборка llm-facing полей из этого делается в
    `search.schema` (дискриминатор по коллекции собирает строку выдачи).
    """

    id: str
    distance: float
    metadata: Mapping[str, str]
    snippet: str
    tags: Sequence[str] = field(default_factory=tuple)
