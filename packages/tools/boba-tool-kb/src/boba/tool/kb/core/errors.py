"""Ошибки postgres KB-плагина."""

from __future__ import annotations

__all__ = [
    "CollectionNotFoundError",
    "KnowledgeBaseError",
]


class KnowledgeBaseError(RuntimeError):
    """База для всех ошибок PostgresKnowledgeBase."""


class CollectionNotFoundError(KeyError):
    """Коллекция с таким именем не зарегистрирована.

    В postgres-модели «коллекция» — это значение колонки `collection` в
    `kb_chunks`. Существование коллекции = факт наличия хотя бы одной
    строки с таким именем ИЛИ запись в `kb_collections` (table-level
    description).
    """

    def __init__(self, name: str) -> None:
        super().__init__(name)
        self.name = name
