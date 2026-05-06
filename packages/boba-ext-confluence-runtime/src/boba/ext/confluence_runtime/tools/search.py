"""Tool: поиск страниц Confluence через REST CQL-search."""

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
    MinValue,
    NonEmpty,
    ParseString,
)
from boba.config.section import ConfigSection
from boba.confluence import ConfluenceConnection
from boba.declaration import FieldSpec, ObjectSchema
from boba.ext.confluence_runtime.http_client import ConfluenceHttpClient
from boba.ext.confluence_runtime.search_pipeline import (
    ConfluenceSearchPipeline,
    ConfluenceSearchPipelineError,
)
from boba.tools.domain import (
    JsonResult,
    ParamOverlay,
    Tool,
    ToolContext,
    ToolExecutionError,
    ToolId,
    ToolResult,
    ToolSourceId,
    param_desc,
    params_field,
)

__all__ = [
    "ConfluenceSearchSection",
    "ConfluenceSearchTool",
    "ConfluenceSearchToolSection",
]


@dataclass(frozen=True)
class ConfluenceSearchConfig:
    """Runtime-конфиг подключения к Confluence для search-операций."""

    base_url: str
    auth_method: str
    auth_user: str
    auth_token: str
    timeout_sec: float


class ConfluenceSearchSection(ConfigSection[ConfluenceSearchConfig]):
    """Секция [ext.confluence.search]: connection-настройки для tool/CLI."""

    namespace: ClassVar[tuple[str, ...]] = ("ext", "confluence", "search")

    schema: ClassVar[ObjectSchema[ConfluenceSearchConfig]] = ObjectSchema(
        description="Поиск по Confluence через REST API (используется tool'ом и CLI).",
        fields=ConfluenceConnection.fields(),
        invariants=ConfluenceConnection.invariant(),
        factory=ConfluenceSearchConfig,
    )


@dataclass(frozen=True)
class SearchArgs:
    query: str
    limit: int


@dataclass(frozen=True)
class ConfluenceSearchToolConfig:
    """DTO секции [ext.confluence.tools.confluence_search]."""

    description: str
    params: Mapping[str, ParamOverlay] = field(default_factory=dict)


class ConfluenceSearchTool(Tool[SearchArgs]):
    """Поиск страниц Confluence по тексту"""

    _ID = ToolId("confluence_search")
    _SOURCE = ToolSourceId("builtin.confluence")

    DEFAULT_DESCRIPTION: ClassVar[str] = (
        "Полнотекстовый поиск страниц Confluence. Возвращает список "
        "(title, space, page_id, url, excerpt)."
    )
    DEFAULT_QUERY_DESC: ClassVar[str] = (
        "Поисковый запрос (обычный текст)."
    )
    DEFAULT_LIMIT_DESC: ClassVar[str] = (
        "Максимум hits в ответе. (обязательно)"
    )

    def __init__(
        self,
        tool_cfg: ConfluenceSearchToolConfig,
        runtime_cfg: ConfluenceSearchConfig,
    ) -> None:
        self._cfg = tool_cfg
        self._runtime = runtime_cfg

    def tool_id(self) -> ToolId:
        return self._ID

    def tool_source_id(self) -> ToolSourceId:
        return self._SOURCE

    def definition(self) -> ObjectSchema[SearchArgs]:
        p = self._cfg.params
        return ObjectSchema(
            description=self._cfg.description,
            fields=[
                FieldSpec(
                    name="query",
                    description=param_desc(p, "query", self.DEFAULT_QUERY_DESC),
                    coercer=ChainCoercer(
                        NonEmpty(),
                        IsString(),
                    ),
                    required=True,
                ),
                FieldSpec(
                    name="limit",
                    description=param_desc(p, "limit", self.DEFAULT_LIMIT_DESC),
                    coercer=ChainCoercer(
                        IsInt(), MinValue(1), MaxValue(50)
                    ),
                    required=True,
                ),
            ],
            factory=SearchArgs,
        )

    def execute(self, ctx: ToolContext, req: SearchArgs) -> ToolResult:
        del ctx

        try:
            with ConfluenceHttpClient.make(self._runtime) as client:
                pipeline = ConfluenceSearchPipeline(
                    client=client,
                    base_url=self._runtime.base_url,
                    cql=self._build_query(req.query),
                    limit=req.limit,
                )
                stats = pipeline.run()
        except ConfluenceSearchPipelineError as e:
            raise ToolExecutionError(
                tool_id=self._ID,
                message=f"Confluence search failed: {type(e).__name__}: {e}",
            ) from e

        return JsonResult(payload={
            "cql": stats.cql,
            "hits": [
                {
                    "page_id": h.page_id,
                    "title": h.title,
                    "space_key": h.space_key,
                    "url": h.url,
                    "excerpt": h.excerpt,
                    "last_modified": h.last_modified,
                }
                for h in stats.hits
            ],
        })

    @staticmethod
    def _build_query(query: str) -> str:
        escaped = query.strip().replace('"', '\\"')
        return f'text ~ "{escaped}"'


class ConfluenceSearchToolSection(ConfigSection[ConfluenceSearchToolConfig]):
    """Секция [ext.confluence.tools.confluence_search]."""

    namespace: ClassVar[tuple[str, ...]] = (
        "ext", "confluence", "tools", "confluence_search",
    )

    schema: ClassVar[ObjectSchema[ConfluenceSearchToolConfig]] = ObjectSchema(
        description="Конфиг tool 'confluence_search'.",
        fields=[
            FieldSpec(
                name="description",
                coercer=ChainCoercer(
                    Default(ConfluenceSearchTool.DEFAULT_DESCRIPTION),
                    ParseString(),
                ),
                description="Override описания tool'а; пусто — дефолт из кода.",
            ),
            params_field("params"),
        ],
        factory=ConfluenceSearchToolConfig,
    )
