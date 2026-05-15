"""Tool: полнотекстовый поиск в одном PG-FTS индексе."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator

from boba.plugin.prompt import PromptOverlay
from boba.tool.postgres_fts.db import PgFtsKnowledgeBase
from boba.tools.domain import (
    JsonResult,
    Tool,
    ToolContext,
    ToolResult,
    ToolSourceId,
)

__all__ = ["FtsSearchArgs", "FtsSearchTool", "FtsSearchToolConfig"]


class FtsSearchArgs(BaseModel):
    """Полнотекстовый поиск в PostgreSQL по whitelist'ed-индексу.

    Возвращает JSON-массив hits {id, score, metadata, snippet}, упорядоченный
    по релевантности (score = ts_rank_cd, больше = ближе). Перед вызовом
    узнай доступные индексы через fts_list_indexes.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    index: str = Field(
        min_length=1,
        description="Имя индекса из fts_list_indexes.",
    )
    query: str = Field(
        min_length=1,
        description=(
            "Поисковый запрос. Поддерживается websearch-синтаксис: "
            'кавычки для фраз ("exact phrase"), OR для альтернатив, '
            "минус-слово для исключения."
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
class FtsSearchToolConfig:
    """DTO tool'а: max_top_k предел + prompt overlay."""

    max_top_k: int
    prompt: PromptOverlay


class FtsSearchTool(Tool[FtsSearchArgs, FtsSearchToolConfig]):
    """Возвращает JSON [{id, score, metadata, snippet}] top-k hits."""

    def __init__(
        self,
        kb: PgFtsKnowledgeBase,
        cfg: FtsSearchToolConfig,
        ctx: Any,
        source_id: ToolSourceId,
    ) -> None:
        super().__init__(cfg, ctx, source_id)
        self._kb = kb

    def _validation_context(self) -> dict[str, Any]:
        """Прокидываем runtime-лимит `max_top_k` в `@field_validator`."""
        return {"max_top_k": self._cfg.max_top_k}

    def execute(self, ctx: ToolContext, req: FtsSearchArgs) -> ToolResult:
        del ctx
        hits = self._kb.search(
            tool_id=self.tool_id(),
            index=req.index,
            query=req.query,
            top_k=req.top_k,
        )
        payload = [
            {
                "id": h.id,
                "score": h.score,
                "metadata": dict(h.metadata),
                "snippet": h.snippet,
            }
            for h in hits
        ]
        return JsonResult(payload=payload)
