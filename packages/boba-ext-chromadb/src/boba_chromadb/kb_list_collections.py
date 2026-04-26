"""Tool: показать агенту список доступных KB-коллекций."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from boba.domain.core.patterns import Converter
from boba.domain.core.tools import (
    Pass,
    Tool,
    ToolContext,
    ToolDefinition,
    ToolId,
    ObjectSchema,
    ToolResult,
    ToolSourceId,
)
from boba_chromadb._kb import ChromaKnowledgeBase


@dataclass(frozen=True)
class KbListCollectionsArgs:
    """Без параметров — kb_list_collections аргументов не принимает."""


class KbListCollectionsArgsConverter(
    Converter[dict[str, Any], KbListCollectionsArgs]
):
    def convert(self, value: dict[str, Any]) -> KbListCollectionsArgs:
        return KbListCollectionsArgs()


class KbListCollectionsTool(Tool[KbListCollectionsArgs]):
    """Возвращает JSON со списком ``[{name, description}]`` коллекций.

    Описание берётся из ``metadata["description"]``, который оператор
    задаёт при создании коллекции (см. README).
    """

    _ID = ToolId("kb_list_collections")
    _SOURCE = ToolSourceId("ext.chromadb")

    def __init__(self, kb: ChromaKnowledgeBase) -> None:
        self._kb = kb

    def tool_id(self) -> ToolId:
        return self._ID

    def tool_source_id(self) -> ToolSourceId:
        return self._SOURCE

    def typed_args_converter(
        self,
    ) -> Converter[dict[str, Any], KbListCollectionsArgs]:
        return KbListCollectionsArgsConverter()

    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            description=(
                "Список доступных knowledge-base коллекций ChromaDB. "
                "Возвращает JSON-массив объектов "
                "{name, description}. Используй перед kb_search чтобы "
                "выбрать подходящую коллекцию."
            ),
            input_schema=ObjectSchema(fields=[], invariants=Pass()),
        )

    def execute(
        self, ctx: ToolContext, req: KbListCollectionsArgs
    ) -> ToolResult:
        del ctx, req
        items = [
            {"name": c.name, "description": c.description}
            for c in self._kb.list_collections()
        ]
        return ToolResult(content=json.dumps(items, ensure_ascii=False))
