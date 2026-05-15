"""Tool: показать агенту список доступных KB-коллекций."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ConfigDict

from boba.plugin.prompt import PromptOverlay
from boba.tool.chromadb.kb import ChromaKnowledgeBase
from boba.tools.domain import (
    JsonResult,
    Tool,
    ToolContext,
    ToolResult,
    ToolSourceId,
)

__all__ = [
    "KbListCollectionsArgs",
    "KbListCollectionsTool",
    "KbListCollectionsToolConfig",
]


class KbListCollectionsArgs(BaseModel):
    """Список доступных knowledge-base коллекций ChromaDB.

    Возвращает JSON-массив объектов {name, description}. Используй перед
    kb_search чтобы выбрать подходящую коллекцию.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")


@dataclass(frozen=True)
class KbListCollectionsToolConfig:
    """DTO tool'а: только prompt overlay."""

    prompt: PromptOverlay


class KbListCollectionsTool(
    Tool[KbListCollectionsArgs, KbListCollectionsToolConfig]
):
    """Возвращает JSON [{name, description}] доступных коллекций."""

    def __init__(
        self,
        kb: ChromaKnowledgeBase,
        cfg: KbListCollectionsToolConfig,
        ctx: Any,
        source_id: ToolSourceId,
    ) -> None:
        super().__init__(cfg, ctx, source_id)
        self._kb = kb

    def execute(
        self, ctx: ToolContext, req: KbListCollectionsArgs
    ) -> ToolResult:
        del ctx, req
        items = [
            {"name": c.name, "description": c.description}
            for c in self._kb.list_collections()
        ]
        return JsonResult(payload=items)
