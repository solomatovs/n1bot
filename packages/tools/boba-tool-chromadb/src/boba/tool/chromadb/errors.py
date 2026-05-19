"""Доменные ошибки KB-tools (v2: без `tool_id` в конструкторах)."""

from __future__ import annotations

__all__ = ["CollectionNotFoundError", "KnowledgeBaseError"]


class KnowledgeBaseError(RuntimeError):
    """База для всех ошибок ChromaKnowledgeBase."""


class CollectionNotFoundError(KeyError):
    """Коллекция с таким именем не зарегистрирована в БД."""

    def __init__(self, name: str) -> None:
        super().__init__(name)
        self.name = name
