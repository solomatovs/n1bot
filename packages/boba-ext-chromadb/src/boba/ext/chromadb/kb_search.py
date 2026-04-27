"""Tool: semantic search по одной KB-коллекции."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from boba.domain.core.patterns import Converter
from boba.domain.core.tools import (
    ChainConverter,
    Default,
    FieldSpec,
    IsInt,
    IsString,
    MaxValue,
    MinLength,
    MinValue,
    ObjectSchema,
    Pass,
    Required,
    Tool,
    ToolContext,
    ToolId,
    ToolResult,
    ToolSourceId,
)
from boba.ext.chromadb.config import ChromaExtConfig
from boba.ext.chromadb.kb import ChromaKnowledgeBase


@dataclass(frozen=True)
class KbSearchArgs:
    collection: str
    query: str
    top_k: int


class KbSearchArgsConverter(Converter[dict[str, Any], KbSearchArgs]):
    def convert(self, value: dict[str, Any]) -> KbSearchArgs:
        return KbSearchArgs(
            collection=value["collection"],
            query=value["query"],
            top_k=value["top_k"],
        )


class KbSearchTool(Tool[KbSearchArgs]):
    """Возвращает JSON [{id, distance, metadata, snippet}] top-k hits."""

    _ID = ToolId("kb_search")
    _SOURCE = ToolSourceId("ext.chromadb")

    def __init__(self, kb: ChromaKnowledgeBase, cfg: ChromaExtConfig) -> None:
        self._kb = kb
        self._cfg = cfg

    def tool_id(self) -> ToolId:
        return self._ID

    def tool_source_id(self) -> ToolSourceId:
        return self._SOURCE

    def typed_args_converter(self) -> Converter[dict[str, Any], KbSearchArgs]:
        return KbSearchArgsConverter()

    def definition(self) -> ObjectSchema[dict[str, Any]]:
        return ObjectSchema(
            description=(
                "Semantic search по KB-коллекции ChromaDB. Возвращает "
                "JSON-массив hits {id, distance, metadata, snippet}, "
                "упорядоченный по релевантности (меньшее distance = "
                "ближе). Перед вызовом узнай доступные коллекции через "
                "kb_list_collections."
            ),
            fields=[
                FieldSpec(
                    name="collection",
                    description="Имя коллекции из kb_list_collections.",
                    converter=ChainConverter(Required(), IsString(), MinLength(1)),
                ),
                FieldSpec(
                    name="query",
                    description=(
                        "Поисковый запрос на естественном языке — "
                        "будет преобразован в embedding и сопоставлен "
                        "с документами коллекции."
                    ),
                    converter=ChainConverter(Required(), IsString(), MinLength(1)),
                ),
                FieldSpec(
                    name="top_k",
                    description=(
                        f"Сколько hits вернуть (1..{self._cfg.max_top_k}). "
                        f"По умолчанию 5."
                    ),
                    converter=ChainConverter(
                        Default(5),
                        IsInt(),
                        MinValue(1),
                        MaxValue(self._cfg.max_top_k),
                    ),
                ),
            ],
            invariants=Pass(),
        )

    def execute(self, ctx: ToolContext, req: KbSearchArgs) -> ToolResult:
        del ctx
        hits = self._kb.search(
            tool_id=self._ID,
            collection=req.collection,
            query=req.query,
            top_k=req.top_k,
        )
        payload = [
            {
                "id": h.id,
                "distance": h.distance,
                "metadata": dict(h.metadata),
                "snippet": h.snippet,
            }
            for h in hits
        ]
        return ToolResult(content=json.dumps(payload, ensure_ascii=False))
