"""Локальные DTO и ошибки KB-плагина (response-форматы + exceptions)."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

__all__ = [
    "CollectionNotFoundError",
    "KnowledgeBaseError",
    "SearchHit",
]


class KnowledgeBaseError(RuntimeError):
    """База для всех ошибок PostgresKnowledgeBase."""


class CollectionNotFoundError(KeyError):
    """Коллекция (значение колонки collection в kb_chunks) не зарегистрирована."""

    def __init__(self, name: str) -> None:
        super().__init__(name)
        self.name = name


@dataclass(frozen=True)
class SearchHit:
    """Один результат kb_search_*; distance — cosine или -ts_rank_cd, меньше ближе."""

    distance: float
    metadata: Mapping[str, str]
    format_content: str
