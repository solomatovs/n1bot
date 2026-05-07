"""Tool: поиск страниц Confluence через REST CQL-search."""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from boba.coercion import (
    ChainCoercer,
    IsInt,
    IsString,
    MaxValue,
    MinValue,
    NonEmpty,
    Required,
)
from boba.declaration import FieldSpec, ObjectSchema
from boba.ext.confluence_tools._http_client import ConfluenceHttpClient
from boba.ext.confluence_tools._search_pipeline import (
    ConfluenceSearchPipeline,
    ConfluenceSearchPipelineError,
)
from boba.plugin import ExtensionContext
from boba.plugin.prompt import PromptOverlay
from boba.tools.domain import (
    JsonResult,
    Tool,
    ToolContext,
    ToolExecutionError,
    ToolId,
    ToolName,
    ToolSourceId,
    ToolResult,
)

__all__ = ["ConfluenceSearchTool", "ConfluenceSearchToolConfig"]


@dataclass(frozen=True)
class SearchArgs:
    query: str
    limit: int


@dataclass(frozen=True)
class ConfluenceSearchToolConfig:
    """DTO tool'а: connection-поля + prompt overlay."""

    base_url: str
    auth_method: str
    auth_user: str
    auth_token: str
    timeout_sec: float
    prompt: PromptOverlay


class ConfluenceSearchTool(Tool[SearchArgs]):
    """Поиск страниц Confluence по тексту."""

    _NAME: ClassVar[ToolName] = ToolName("confluence_search")

    def __init__(
        self,
        cfg: ConfluenceSearchToolConfig,
        ctx: ExtensionContext,
        source_id: ToolSourceId,
    ) -> None:
        self._cfg = cfg
        self._ctx = ctx
        self._tool_id = ToolId.compose(source_id, self._NAME)

    def tool_id(self) -> ToolId:
        return self._tool_id


    def definition(self) -> ObjectSchema[SearchArgs]:
        return self._cfg.prompt.apply(ObjectSchema(
            description=(
                "Полнотекстовый поиск страниц Confluence. Возвращает список "
                "(title, space, page_id, url, excerpt)."
            ),
            fields=[
                FieldSpec(
                    name="query",
                    description="Поисковый запрос (обычный текст).",
                    coercer=ChainCoercer(Required(), NonEmpty(), IsString()),
                ),
                FieldSpec(
                    name="limit",
                    description="Максимум hits в ответе.",
                    coercer=ChainCoercer(Required(), IsInt(), MinValue(1), MaxValue(50)),
                ),
            ],
            factory=SearchArgs,
        ))

    def execute(self, ctx: ToolContext, req: SearchArgs) -> ToolResult:
        del ctx
        try:
            with ConfluenceHttpClient.make(self._cfg) as client:
                pipeline = ConfluenceSearchPipeline(
                    client=client,
                    base_url=self._cfg.base_url,
                    cql=self._build_query(req.query),
                    limit=req.limit,
                )
                stats = pipeline.run()
        except ConfluenceSearchPipelineError as e:
            raise ToolExecutionError(
                tool_id=self._tool_id,
                message=f"Confluence search failed: {type(e).__name__}: {e}",
            ) from e

        return JsonResult(
            payload={
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
            }
        )

    @staticmethod
    def _build_query(query: str) -> str:
        escaped = query.strip().replace('"', '\\"')
        return f'text ~ "{escaped}"'
