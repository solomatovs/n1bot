"""Tool: semantic search по одной KB-коллекции."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import ClassVar

from boba.coercion import (
    ChainCoercer,
    Default,
    IsInt,
    IsString,
    MaxValue,
    MinLength,
    MinValue,
    ParseInt,
    ParseString,
)
from boba.config.section import ConfigSection
from boba.declaration import FieldSpec, ObjectSchema
from boba.ext.chromadb_tools.kb import ChromaKnowledgeBase
from boba.tools.domain import (
    JsonResult,
    ParamOverlay,
    Tool,
    ToolContext,
    ToolId,
    ToolResult,
    ToolSourceId,
    param_desc,
    params_field,
)


@dataclass(frozen=True)
class KbSearchArgs:
    collection: str
    query: str
    top_k: int


@dataclass(frozen=True)
class KbSearchToolConfig:
    """DTO секции [ext.chromadb.tools.kb_search]."""

    description: str
    max_top_k: int
    params: Mapping[str, ParamOverlay] = field(default_factory=dict)


class KbSearchTool(Tool[KbSearchArgs]):
    """Возвращает JSON [{id, distance, metadata, snippet}] top-k hits."""

    _ID = ToolId("kb_search")
    _SOURCE = ToolSourceId("ext.chromadb_tools")

    DEFAULT_DESCRIPTION: ClassVar[str] = (
        "Semantic search по KB-коллекции ChromaDB. Возвращает "
        "JSON-массив hits {id, distance, metadata, snippet}, "
        "упорядоченный по релевантности (меньшее distance = "
        "ближе). Перед вызовом узнай доступные коллекции через "
        "kb_list_collections."
    )
    DEFAULT_COLLECTION_DESC: ClassVar[str] = "Имя коллекции из kb_list_collections."
    DEFAULT_QUERY_DESC: ClassVar[str] = (
        "Поисковый запрос на естественном языке — "
        "будет преобразован в embedding и сопоставлен "
        "с документами коллекции."
    )
    DEFAULT_MAX_TOP_K: ClassVar[int] = 20

    def __init__(
        self,
        kb: ChromaKnowledgeBase,
        cfg: KbSearchToolConfig,
    ) -> None:
        self._kb = kb
        self._cfg = cfg

    def tool_id(self) -> ToolId:
        return self._ID

    def tool_source_id(self) -> ToolSourceId:
        return self._SOURCE

    def definition(self) -> ObjectSchema[KbSearchArgs]:
        p = self._cfg.params
        max_top_k = self._cfg.max_top_k
        default_top_k_desc = f"Сколько hits вернуть (1..{max_top_k}). По умолчанию 5."
        return ObjectSchema(
            description=self._cfg.description,
            fields=[
                FieldSpec(
                    name="collection",
                    description=param_desc(
                        p, "collection", self.DEFAULT_COLLECTION_DESC
                    ),
                    coercer=ChainCoercer(IsString(), MinLength(1)),
                    required=True,
                ),
                FieldSpec(
                    name="query",
                    description=param_desc(p, "query", self.DEFAULT_QUERY_DESC),
                    coercer=ChainCoercer(IsString(), MinLength(1)),
                    required=True,
                ),
                FieldSpec(
                    name="top_k",
                    description=param_desc(p, "top_k", default_top_k_desc),
                    coercer=ChainCoercer(
                        Default(5),
                        IsInt(),
                        MinValue(1),
                        MaxValue(max_top_k),
                    ),
                ),
            ],
            factory=KbSearchArgs,
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


class KbSearchToolSection(ConfigSection[KbSearchToolConfig]):
    """Секция [ext.chromadb.tools.kb_search]."""

    namespace: ClassVar[tuple[str, ...]] = (
        "ext",
        "chromadb",
        "tools",
        "kb_search",
    )

    schema: ClassVar[ObjectSchema[KbSearchToolConfig]] = ObjectSchema(
        description="Конфиг tool 'kb_search'.",
        fields=[
            FieldSpec(
                name="description",
                coercer=ChainCoercer(
                    Default(KbSearchTool.DEFAULT_DESCRIPTION), ParseString()
                ),
                description="Override описания tool'а; пусто — дефолт из кода.",
            ),
            FieldSpec(
                name="max_top_k",
                coercer=ChainCoercer(
                    Default(KbSearchTool.DEFAULT_MAX_TOP_K),
                    ParseInt(),
                    MinValue(1),
                ),
                description=(
                    "Жёсткий потолок параметра top_k (защита от слишком "
                    "крупных запросов)."
                ),
            ),
            params_field("params"),
        ],
        factory=KbSearchToolConfig,
    )
