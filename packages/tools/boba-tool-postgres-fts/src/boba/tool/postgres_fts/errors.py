"""Доменные ошибки PG-FTS tools."""

from __future__ import annotations

from boba.tools.domain import ToolExecutionError, ToolId

__all__ = ["FtsKnowledgeBaseError", "IndexNotFoundError"]


class FtsKnowledgeBaseError(ToolExecutionError):
    """База для всех ошибок PgFtsKnowledgeBase."""


class IndexNotFoundError(FtsKnowledgeBaseError):
    """Индекс не зарегистрирован в whitelist'е плагина."""

    def __init__(self, tool_id: ToolId, name: str) -> None:
        super().__init__(
            tool_id,
            f"fts index {name!r} not found; "
            f"call fts_list_indexes to see available ones",
        )
        self.name = name
