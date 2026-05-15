"""Tool: полнотекстовый поиск в одном PG-FTS индексе."""

from __future__ import annotations

from dataclasses import dataclass
from functools import cached_property
from typing import Annotated, Any

from boba.plugin.prompt import PromptOverlay
from boba.schema.coercion import (
    ChainCoercer,
    Default,
    IsInt,
    IsString,
    MaxValue,
    MinLength,
    MinValue,
    Required,
)
from boba.schema.declaration import FieldSpec, ObjectSchema
from boba.tool.postgres_fts.db import PgFtsKnowledgeBase
from boba.tools.domain import (
    JsonResult,
    Tool,
    ToolContext,
    ToolResult,
    ToolSourceId,
    ToolWireSchemaBuilder,
)
from boba.tools.domain.llm_schema import clean_llm_json_schema
from boba.tools.domain.tool import JsonSchemaOverlay, _ToolArgsAdapter

__all__ = ["FtsSearchArgs", "FtsSearchTool", "FtsSearchToolConfig"]


@dataclass(frozen=True)
class FtsSearchArgs:
    """Полнотекстовый поиск в PostgreSQL по whitelist'ed-индексу.

    Возвращает JSON-массив hits {id, score, metadata, snippet}, упорядоченный
    по релевантности (score = ts_rank_cd, больше = ближе). Перед вызовом
    узнай доступные индексы через fts_list_indexes.
    """

    index: Annotated[str, "Имя индекса из fts_list_indexes.", MinLength(1)]
    query: Annotated[
        str,
        "Поисковый запрос. Поддерживается websearch-синтаксис: "
        'кавычки для фраз ("exact phrase"), OR для альтернатив, '
        "минус-слово для исключения.",
        MinLength(1),
    ]
    top_k: Annotated[int, "Сколько hits вернуть. По умолчанию 5.", MinValue(1)] = 5


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

    @cached_property
    def _legacy_schema(self) -> ObjectSchema[FtsSearchArgs]:
        """Canonical ObjectSchema c runtime max_top_k. Используется и в
        `definition()`-эмите, и в `_args_adapter`-валидации."""
        max_top_k = self._cfg.max_top_k
        return ObjectSchema(
            description=FtsSearchArgs.__doc__ or "",
            fields=[
                FieldSpec(
                    name="index",
                    description="Имя индекса из fts_list_indexes.",
                    coercer=ChainCoercer(Required(), IsString(), MinLength(1)),
                ),
                FieldSpec(
                    name="query",
                    description=(
                        "Поисковый запрос. Поддерживается websearch-синтаксис: "
                        'кавычки для фраз ("exact phrase"), OR для '
                        "альтернатив, минус-слово для исключения."
                    ),
                    coercer=ChainCoercer(Required(), IsString(), MinLength(1)),
                ),
                FieldSpec(
                    name="top_k",
                    description=(
                        f"Сколько hits вернуть (1..{max_top_k}). По умолчанию 5."
                    ),
                    coercer=ChainCoercer(
                        Default(5),
                        IsInt(),
                        MinValue(1),
                        MaxValue(max_top_k),
                    ),
                ),
            ],
            factory=FtsSearchArgs,
        )

    def definition(self) -> dict[str, Any]:
        wire = ToolWireSchemaBuilder(self._legacy_schema).build()
        prompt = self._cfg.prompt
        if isinstance(prompt, JsonSchemaOverlay):
            wire = prompt.apply_to_json_schema(wire)
        return clean_llm_json_schema(wire)

    @property
    def _args_adapter(  # type: ignore[override]
        self,
    ) -> _ToolArgsAdapter[FtsSearchArgs]:
        """Валидация через тот же `_legacy_schema` (с MaxValue(max_top_k))."""
        return _ToolArgsAdapter(self._legacy_schema, self.tool_id())

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
