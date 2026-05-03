"""Tool: показать агенту список доступных KB-коллекций."""

from __future__ import annotations

import json
from dataclasses import dataclass

from boba_next.declaration import ObjectSchema
from boba_next.tools import Tool, ToolContext, ToolId, ToolResult, ToolSourceId

from boba.ext.chromadb.kb import ChromaKnowledgeBase


@dataclass(frozen=True)
class KbListCollectionsArgs:
    """Без параметров — kb_list_collections аргументов не принимает."""


class KbListCollectionsTool(Tool[KbListCollectionsArgs]):
    """Возвращает JSON [{name, description}] доступных коллекций."""

    _ID = ToolId("kb_list_collections")
    _SOURCE = ToolSourceId("ext.chromadb")

    def __init__(self, kb: ChromaKnowledgeBase) -> None:
        self._kb = kb

    def tool_id(self) -> ToolId:
        return self._ID

    def tool_source_id(self) -> ToolSourceId:
        return self._SOURCE

    def definition(self) -> ObjectSchema[KbListCollectionsArgs]:
        return ObjectSchema(
            description=(
                "Список доступных knowledge-base коллекций ChromaDB. "
                "Возвращает JSON-массив объектов "
                "{name, description}. Используй перед kb_search чтобы "
                "выбрать подходящую коллекцию."
            ),
            fields=[],
            factory=KbListCollectionsArgs,
        )

    def execute(self, ctx: ToolContext, req: KbListCollectionsArgs) -> ToolResult:
        del ctx, req
        items = [
            {"name": c.name, "description": c.description}
            for c in self._kb.list_collections()
        ]
        return ToolResult(content=json.dumps(items, ensure_ascii=False))
