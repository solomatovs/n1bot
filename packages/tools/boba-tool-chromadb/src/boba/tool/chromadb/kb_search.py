"""Tool: semantic search по одной KB-коллекции."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator

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

__all__ = ["KbSearchArgs", "KbSearchTool", "KbSearchToolConfig"]


class KbSearchArgs(BaseModel):
    """Semantic search по KB-коллекции ChromaDB.

    Возвращает JSON-массив hits {id, distance, metadata, snippet}, упорядоченный
    по релевантности (меньшее distance = ближе). Перед вызовом узнай доступные
    коллекции через kb_list_collections.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    collection: str = Field(
        min_length=1,
        description="Имя коллекции из kb_list_collections.",
    )
    query: str = Field(
        min_length=1,
        description=(
            "Поисковый запрос на естественном языке — будет преобразован "
            "в embedding и сопоставлен с документами коллекции."
        ),
    )
    top_k: int = Field(
        default=5,
        ge=1,
        description=(
            "Сколько hits вернуть. По умолчанию 5; жёсткий потолок задан "
            "в конфиге плагина (`max_top_k`)."
        ),
    )

    @field_validator("top_k", mode="after")
    @classmethod
    def _check_max_top_k(cls, v: int, info: ValidationInfo) -> int:
        """Runtime-проверка верхней границы из `info.context['max_top_k']`."""
        ctx = info.context
        if isinstance(ctx, Mapping):
            limit = ctx.get("max_top_k")
            if isinstance(limit, int) and v > limit:
                msg = f"top_k={v} превышает max_top_k={limit}"
                raise ValueError(msg)
        return v


@dataclass(frozen=True)
class KbSearchToolConfig:
    """DTO tool'а: все cfg-поля, чтобы tool построил KB сам."""

    persist_path: str
    snippet_chars: int
    embedding_model: str
    embedding_base_url: str
    embedding_api_key: str
    max_top_k: int
    prompt: PromptOverlay


class KbSearchTool(Tool[KbSearchArgs, KbSearchToolConfig]):
    """Возвращает JSON [{id, distance, metadata, snippet}] top-k hits.

    `ChromaKnowledgeBase` собирается lazily внутри tool через
    process-singleton `get_knowledge_base` — один и тот же client на
    persist_path для read- и write-сторон.
    """

    def __init__(
        self,
        cfg: KbSearchToolConfig,
        ctx: Any,
        source_id: ToolSourceId,
    ) -> None:
        super().__init__(cfg, ctx, source_id)
        self._kb: ChromaKnowledgeBase | None = None

    def _validation_context(self) -> dict[str, Any]:
        """Прокидываем runtime-лимит `max_top_k` в `@field_validator`."""
        return {"max_top_k": self._cfg.max_top_k}

    def execute(self, ctx: ToolContext, req: KbSearchArgs) -> ToolResult:
        del ctx
        hits = self._get_or_build_kb().search(
            tool_id=self.tool_id(),
            collection=req.collection,
            query=req.query,
            top_k=req.top_k,
        )
        payload = [
            {
                "id": h.id,
                "distance": h.distance,
                "link": self._build_link(h.metadata),
                "metadata": dict(h.metadata),
                "snippet": h.snippet,
            }
            for h in hits
        ]
        return JsonResult(payload=payload)

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

    @staticmethod
    def _build_link(metadata: Mapping[str, str]) -> str:
        """source_url[#anchor] — готовый deep-link, чтобы агент не склеивал сам."""
        url = str(metadata.get("source_url") or "")
        if not url:
            return ""
        anchor = str(metadata.get("anchor") or "")
        return f"{url}#{anchor}" if anchor else url
