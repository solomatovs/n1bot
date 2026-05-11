"""Tool: semantic search по одной KB-коллекции."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import ClassVar

from boba.plugin import ExtensionContext
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
from boba.tool.chromadb.kb import ChromaKnowledgeBase
from boba.tools.domain import (
    JsonResult,
    Tool,
    ToolContext,
    ToolId,
    ToolName,
    ToolResult,
    ToolSourceId,
)

__all__ = ["KbSearchTool", "KbSearchToolConfig"]


@dataclass(frozen=True)
class KbSearchArgs:
    collection: str
    query: str
    top_k: int


@dataclass(frozen=True)
class KbSearchToolConfig:
    """DTO tool'а: max_top_k предел + prompt overlay."""

    max_top_k: int
    prompt: PromptOverlay


class KbSearchTool(Tool[KbSearchArgs]):
    """Возвращает JSON [{id, distance, metadata, snippet}] top-k hits."""

    _NAME: ClassVar[ToolName] = ToolName("kb_search")

    def __init__(
        self,
        kb: ChromaKnowledgeBase,
        cfg: KbSearchToolConfig,
        ctx: ExtensionContext,
        source_id: ToolSourceId,
    ) -> None:
        self._kb = kb
        self._cfg = cfg
        self._ctx = ctx
        self._tool_id = ToolId.compose(source_id, self._NAME)

    def tool_id(self) -> ToolId:
        return self._tool_id


    def definition(self) -> ObjectSchema[KbSearchArgs]:
        max_top_k = self._cfg.max_top_k
        return self._cfg.prompt.apply(ObjectSchema(
            description=(
                "Semantic search по KB-коллекции ChromaDB. Возвращает "
                "JSON-массив hits {id, distance, metadata, snippet}, "
                "упорядоченный по релевантности (меньшее distance = ближе). "
                "Перед вызовом узнай доступные коллекции через "
                "kb_list_collections."
            ),
            fields=[
                FieldSpec(
                    name="collection",
                    description="Имя коллекции из kb_list_collections.",
                    coercer=ChainCoercer(Required(), IsString(), MinLength(1)),
                ),
                FieldSpec(
                    name="query",
                    description=(
                        "Поисковый запрос на естественном языке — "
                        "будет преобразован в embedding и сопоставлен с "
                        "документами коллекции."
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
            factory=KbSearchArgs,
        ))

    def execute(self, ctx: ToolContext, req: KbSearchArgs) -> ToolResult:
        del ctx
        hits = self._kb.search(
            tool_id=self._tool_id,
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

    @staticmethod
    def _build_link(metadata: Mapping[str, str]) -> str:
        """source_url[#anchor] — готовый deep-link, чтобы агент не склеивал сам."""
        url = str(metadata.get("source_url") or "")
        if not url:
            return ""
        anchor = str(metadata.get("anchor") or "")
        return f"{url}#{anchor}" if anchor else url
