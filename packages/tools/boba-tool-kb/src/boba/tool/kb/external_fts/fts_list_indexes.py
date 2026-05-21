"""Tool: список whitelist-FTS-индексов оператора (вне `kb_chunks`)."""

from __future__ import annotations

from typing import Annotated, Any

from boba.tool.kb.external_fts.db import PgFtsKnowledgeBase
from boba.tools import FromDI, Scope, tool

__all__ = ["fts_list_indexes"]


@tool
def fts_list_indexes(
    kb: Annotated[PgFtsKnowledgeBase, FromDI(Scope.APP)],
) -> list[dict[str, Any]]:
    """Список whitelist-FTS-индексов оператора (для `fts_search`).

    Это НЕ список коллекций нашей KB (для них — `kb_list_collections`).
    Здесь — внешние индексы, описанные в `[tool.kb.external_fts].indexes`:
    поверх чужих таблиц БД оператора, read-only.

    Возвращает JSON-массив объектов {name, description}. Используй перед
    `fts_search`, чтобы выбрать подходящий индекс по описанию.
    """
    return [
        {"name": i.name, "description": i.description}
        for i in kb.list_indexes()
    ]
