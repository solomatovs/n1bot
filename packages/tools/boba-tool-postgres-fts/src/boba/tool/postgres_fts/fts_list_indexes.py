"""Tool: показать агенту список доступных FTS-индексов."""

from __future__ import annotations

from typing import Annotated, Any

from boba.tool.postgres_fts.db import PgFtsKnowledgeBase
from boba.tools import FromDI, Scope, tool

__all__ = ["fts_list_indexes"]


@tool
def fts_list_indexes(
    kb: Annotated[PgFtsKnowledgeBase, FromDI(Scope.APP)],
) -> list[dict[str, Any]]:
    """Список доступных PostgreSQL FTS-индексов.

    Возвращает JSON-массив объектов {name, description}. Используй перед
    fts_search чтобы выбрать подходящий индекс.
    """
    return [
        {"name": i.name, "description": i.description}
        for i in kb.list_indexes()
    ]
