"""Tool: поиск страниц Confluence через REST CQL-search."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated

from boba.plugin.prompt import PromptOverlay
from boba.schema.coercion import MaxValue, MinValue, NonEmpty
from boba.tool.confluence._http_client import ConfluenceHttpClient
from boba.tool.confluence._search_pipeline import (
    ConfluenceSearchPipeline,
    ConfluenceSearchPipelineError,
)
from boba.tools.domain import (
    JsonResult,
    Tool,
    ToolContext,
    ToolExecutionError,
    ToolResult,
)

__all__ = ["ConfluenceSearchTool", "ConfluenceSearchToolConfig", "SearchArgs"]


@dataclass(frozen=True)
class SearchArgs:
    """Полнотекстовый поиск страниц Confluence.

    Возвращает список (title, space, page_id, url, excerpt).
    """

    query: Annotated[str, "Поисковый запрос (обычный текст).", NonEmpty()]
    limit: Annotated[int, "Максимум hits в ответе.", MinValue(1), MaxValue(50)]


@dataclass(frozen=True)
class ConfluenceSearchToolConfig:
    """DTO tool'а: connection-поля + prompt overlay."""

    base_url: str
    auth_method: str
    auth_user: str
    auth_token: str
    timeout_sec: float
    prompt: PromptOverlay


class ConfluenceSearchTool(Tool[SearchArgs, ConfluenceSearchToolConfig]):
    """Поиск страниц Confluence по тексту."""

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
                tool_id=self.tool_id(),
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
