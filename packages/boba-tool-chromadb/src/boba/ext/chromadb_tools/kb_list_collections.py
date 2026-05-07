"""Tool: показать агенту список доступных KB-коллекций."""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from boba.declaration import ObjectSchema
from boba.ext.chromadb_tools.kb import ChromaKnowledgeBase
from boba.plugin import ExtensionContext
from boba.plugin.prompt import PromptOverlay
from boba.tools.domain import (
    JsonResult,
    Tool,
    ToolContext,
    ToolId,
    ToolName,
    ToolSourceId,
    ToolResult,
)

__all__ = ["KbListCollectionsTool", "KbListCollectionsToolConfig"]


@dataclass(frozen=True)
class KbListCollectionsArgs:
    """Без параметров — kb_list_collections аргументов не принимает."""


@dataclass(frozen=True)
class KbListCollectionsToolConfig:
    """DTO tool'а: только prompt overlay."""

    prompt: PromptOverlay


class KbListCollectionsTool(Tool[KbListCollectionsArgs]):
    """Возвращает JSON [{name, description}] доступных коллекций."""

    _NAME: ClassVar[ToolName] = ToolName("kb_list_collections")

    def __init__(
        self,
        kb: ChromaKnowledgeBase,
        cfg: KbListCollectionsToolConfig,
        ctx: ExtensionContext,
        source_id: ToolSourceId,
    ) -> None:
        self._kb = kb
        self._cfg = cfg
        self._ctx = ctx
        self._tool_id = ToolId.compose(source_id, self._NAME)

    def tool_id(self) -> ToolId:
        return self._tool_id


    def definition(self) -> ObjectSchema[KbListCollectionsArgs]:
        return self._cfg.prompt.apply(ObjectSchema(
            description=(
                "Список доступных knowledge-base коллекций ChromaDB. "
                "Возвращает JSON-массив объектов {name, description}. "
                "Используй перед kb_search чтобы выбрать подходящую коллекцию."
            ),
            fields=[],
            factory=KbListCollectionsArgs,
        ))

    def execute(self, ctx: ToolContext, req: KbListCollectionsArgs) -> ToolResult:
        del ctx, req
        items = [
            {"name": c.name, "description": c.description}
            for c in self._kb.list_collections()
        ]
        return JsonResult(payload=items)
