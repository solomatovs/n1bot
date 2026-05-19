"""Tool: показать агенту список доступных KB-коллекций."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ConfigDict

from boba.plugin.prompt import PromptOverlay
from boba.tool.chromadb.embedder_factory import (
    build_chromadb_embedding_function,
)
from boba.tool.chromadb.kb import ChromaKnowledgeBase, get_knowledge_base
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
    """DTO tool'а: cfg-поля для самостоятельной сборки KB + prompt overlay."""

    persist_path: str
    snippet_chars: int
    embedding_model: str
    embedding_base_url: str
    embedding_api_key: str
    prompt: PromptOverlay


class KbListCollectionsTool(
    Tool[KbListCollectionsArgs, KbListCollectionsToolConfig]
):
    """Возвращает JSON [{name, description}] доступных коллекций."""

    def __init__(
        self,
        cfg: KbListCollectionsToolConfig,
        ctx: Any,
        source_id: ToolSourceId,
    ) -> None:
        super().__init__(cfg, ctx, source_id)
        self._kb: ChromaKnowledgeBase | None = None

    def execute(
        self, ctx: ToolContext, req: KbListCollectionsArgs,
    ) -> ToolResult:
        del ctx, req
        items = [
            {"name": c.name, "description": c.description}
            for c in self._get_or_build_kb().list_collections()
        ]
        return JsonResult(payload=items)

    def _get_or_build_kb(self) -> ChromaKnowledgeBase:
        if self._kb is None:
            self._kb = get_knowledge_base(
                self._cfg.persist_path,
                self._cfg.snippet_chars,
                embedding_function=build_chromadb_embedding_function(
                    model=self._cfg.embedding_model,
                    base_url=self._cfg.embedding_base_url,
                    api_key=self._cfg.embedding_api_key,
                ),
            )
        return self._kb
