"""Доменные ошибки KB-tools."""

from __future__ import annotations

from boba.domain.core.tools import ToolExecutionError, ToolId


class KnowledgeBaseError(ToolExecutionError):
    """База для всех ошибок ChromaKnowledgeBase."""


class CollectionNotFoundError(KnowledgeBaseError):
    """Коллекция с таким именем не зарегистрирована в БД."""

    def __init__(self, tool_id: ToolId, name: str) -> None:
        super().__init__(
            tool_id,
            f"collection {name!r} not found; "
            f"call kb_list_collections to see available ones",
        )
        self.name = name
