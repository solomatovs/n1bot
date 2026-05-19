"""Tool: показать агенту список доступных KB-коллекций."""

from __future__ import annotations

from typing import Annotated, Any

from boba.tool.chromadb.enable import chromadb_enable_if
from boba.tool.chromadb.kb import ChromaKnowledgeBase
from boba.tools import FromDI, Scope, tool

__all__ = ["KbListCollectionsTool"]


@tool(enable_if=chromadb_enable_if("kb_list_collections"))
class KbListCollectionsTool:
    """Список доступных knowledge-base коллекций ChromaDB.

    Возвращает JSON-массив объектов {name, description}. Используй перед
    kb_search чтобы выбрать подходящую коллекцию.
    """

    def __call__(
        self,
        kb: Annotated[ChromaKnowledgeBase, FromDI(Scope.APP)],
    ) -> list[dict[str, Any]]:
        return [
            {"name": c.name, "description": c.description}
            for c in kb.list_collections()
        ]
