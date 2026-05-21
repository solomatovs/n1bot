"""Tool: показать агенту список доступных KB-коллекций (postgres)."""

from __future__ import annotations

from typing import Annotated, Any

from boba.tool.postgres.kb import PostgresKnowledgeBase
from boba.tools import FromDI, Scope, tool

__all__ = ["kb_list_collections"]


@tool
def kb_list_collections(
    kb: Annotated[PostgresKnowledgeBase, FromDI(Scope.APP)],
) -> list[dict[str, Any]]:
    """Список доступных knowledge-base коллекций (postgres + pgvector).

    Возвращает JSON-массив объектов {name, description}. Используй перед
    kb_search чтобы выбрать подходящую коллекцию.
    """
    return [
        {"name": c.name, "description": c.description}
        for c in kb.list_collections()
    ]
