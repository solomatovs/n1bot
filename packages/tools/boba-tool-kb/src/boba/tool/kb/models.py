"""Локальные DTO postgres KB-плагина (response-форматы для tools)."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

__all__ = [
    "CollectionInfo",
    "SearchHit",
]


@dataclass(frozen=True)
class CollectionInfo:
    """Сводка одной коллекции для kb_list_collections."""

    name: str
    description: str


@dataclass(frozen=True)
class SearchHit:
    """Один результат kb_search.

    `distance` в гибридной выдаче не имеет «единого» физического смысла:
    это **отрицательный RRF-скор** (`-rrf`), чтобы сохранить семантику
    «меньше = ближе/релевантнее», к которой привыкли потребители
    `boba.indexing.SearchHit`. Чистый cosine-distance остаётся на стороне
    `PostgresVectorStore.similarity_search` (vector-only путь).
    """

    id: str
    distance: float
    metadata: Mapping[str, str]
    snippet: str
