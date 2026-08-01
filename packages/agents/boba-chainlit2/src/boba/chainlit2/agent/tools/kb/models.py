"""Локальные DTO и ошибки KB-плагина (response-форматы + exceptions)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

__all__ = [
    "CollectionNotFoundError",
    "KnowledgeBaseError",
    "SearchHit",
]


class KnowledgeBaseError(RuntimeError):
    """База для всех ошибок PostgresKnowledgeBase."""


class CollectionNotFoundError(KeyError):
    """Коллекция с таким именем не зарегистрирована.

    В postgres-модели «коллекция» — это значение колонки collection в
    kb_chunks. Существование коллекции = факт наличия хотя бы одной
    строки с таким именем ИЛИ запись в kb_collections (table-level
    description).
    """

    def __init__(self, name: str) -> None:
        super().__init__(name)
        self.name = name


@dataclass(frozen=True)
class SearchHit:
    """Один результат kb_search_* (vector/fts).

    distance — cosine-distance (vector_search) либо отрицательный
    ts_rank_cd-скор (fts_search); в обоих случаях семантика
    «меньше = ближе/релевантнее».

    metadata — сырой набор ключей чанка (все слои pipeline'а), tags —
    колонка kb_chunks.tags. Сборка llm-facing полей из этого делается в
    search.schema (дискриминатор по коллекции собирает строку выдачи).
    """

    id: str
    distance: float
    metadata: Mapping[str, str]
    snippet: str
    tags: Sequence[str] = field(default_factory=tuple)
