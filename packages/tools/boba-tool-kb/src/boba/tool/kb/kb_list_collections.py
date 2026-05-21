"""Tool: показать агенту список доступных KB-коллекций (postgres)."""

from __future__ import annotations

from typing import Annotated, Any

from boba.tool.kb.kb import PostgresKnowledgeBase
from boba.tools import FromDI, Scope, tool

__all__ = ["kb_list_collections"]


@tool
def kb_list_collections(
    kb: Annotated[PostgresKnowledgeBase, FromDI(Scope.APP)],
) -> list[dict[str, Any]]:
    """Список коллекций нашей KB (для `kb_search`).

    Это коллекции внутри `kb_chunks` — то, что наполнено `kb_ingest` /
    `kb_ingest_confluence`. НЕ список whitelist-индексов оператора (для
    них — `fts_list_indexes`).

    Возвращает JSON-массив объектов {name, description}. Используй перед
    `kb_search`, чтобы выбрать подходящую коллекцию по описанию.
    """
    return [
        {"name": c.name, "description": c.description}
        for c in kb.list_collections()
    ]
