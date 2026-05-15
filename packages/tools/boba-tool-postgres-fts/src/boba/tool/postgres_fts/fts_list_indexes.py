"""Tool: показать агенту список доступных FTS-индексов."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ConfigDict

from boba.plugin.prompt import PromptOverlay
from boba.tool.postgres_fts.db import PgFtsKnowledgeBase
from boba.tools.domain import (
    JsonResult,
    Tool,
    ToolContext,
    ToolResult,
    ToolSourceId,
)

__all__ = [
    "FtsListIndexesArgs",
    "FtsListIndexesTool",
    "FtsListIndexesToolConfig",
]


class FtsListIndexesArgs(BaseModel):
    """Список доступных PostgreSQL FTS-индексов.

    Возвращает JSON-массив объектов {name, description}. Используй перед
    fts_search чтобы выбрать подходящий индекс.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")


@dataclass(frozen=True)
class FtsListIndexesToolConfig:
    """DTO tool'а: только prompt overlay."""

    prompt: PromptOverlay


class FtsListIndexesTool(Tool[FtsListIndexesArgs, FtsListIndexesToolConfig]):
    """Возвращает JSON [{name, description}] зарегистрированных FTS-индексов."""

    def __init__(
        self,
        kb: PgFtsKnowledgeBase,
        cfg: FtsListIndexesToolConfig,
        ctx: Any,
        source_id: ToolSourceId,
    ) -> None:
        super().__init__(cfg, ctx, source_id)
        self._kb = kb

    def execute(self, ctx: ToolContext, req: FtsListIndexesArgs) -> ToolResult:
        del ctx, req
        items = [
            {"name": i.name, "description": i.description}
            for i in self._kb.list_indexes()
        ]
        return JsonResult(payload=items)
