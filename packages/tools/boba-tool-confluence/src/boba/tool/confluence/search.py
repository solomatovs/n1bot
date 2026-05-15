"""Tool: поиск страниц Confluence через REST CQL-search."""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar
from urllib.parse import quote

import httpx
from pydantic import BaseModel, ConfigDict, Field

from boba.indexing import (
    PipelineContext,
    PipelineId,
    ReaderKeys,
    RuntimePipeline,
    Section,
)
from boba.plugin.prompt import PromptOverlay
from boba.tool.confluence.connection import (
    ConfluenceConnection,
)
from boba.tool.confluence.keys import ConfluenceKeys
from boba.tool.confluence.request_sources.search import (
    ConfluenceCqlSearchRequestSource,
)
from boba.tool.confluence.search_reader import ConfluenceSearchHitsReader
from boba.tools.domain import (
    JsonResult,
    Tool,
    ToolContext,
    ToolExecutionError,
    ToolResult,
)
from boba.transport.http import HttpKeys

__all__ = ["ConfluenceSearchTool", "ConfluenceSearchToolConfig", "SearchArgs"]


class SearchArgs(BaseModel):
    """Полнотекстовый поиск страниц Confluence.

    Возвращает список (title, space, page_id, url, excerpt).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    query: str = Field(
        min_length=1,
        description="Строка понотекстового поиска в confluence",
    )
    space: str | None = Field(
        default=None,
        description="Ограничение поиска по space",
    )
    limit: int = Field(ge=1, le=50, description="Максимум hits в ответе.")


@dataclass(frozen=True)
class ConfluenceSearchToolConfig:
    """DTO tool'а: connection-поля + prompt overlay."""

    base_url: str
    auth_method: str
    auth_user: str
    auth_token: str
    timeout_sec: float
    ssl_verify: bool
    prompt: PromptOverlay


class ConfluenceSearchTool(Tool[SearchArgs, ConfluenceSearchToolConfig]):
    """Поиск страниц Confluence по тексту."""

    _PIPELINE_ID: ClassVar[PipelineId] = PipelineId("confluence.search")
    _SNIPPET_CHARS: ClassVar[int] = 300

    def execute(self, ctx: ToolContext, req: SearchArgs) -> ToolResult:
        del ctx
        pipeline: RuntimePipeline = RuntimePipeline(
            request_source=ConfluenceCqlSearchRequestSource(
                base_url=self._cfg.base_url,
                auth=ConfluenceConnection.make_auth(self._cfg),
                cql=self._build_query(
                    query=req.query,
                    space=req.space,
                ),
                limit=req.limit,
            ),
            transport=ConfluenceConnection.make_transport(self._cfg),
            reader=ConfluenceSearchHitsReader(
                base_url=self._cfg.base_url,
                snippet_chars=self._SNIPPET_CHARS,
            ),
        )

        try:
            sections = list(
                pipeline.stream(PipelineContext(pipeline_id=self._PIPELINE_ID))
            )
        except httpx.HTTPError as e:
            raise ToolExecutionError(
                tool_id=self.tool_id(),
                message=f"Confluence search failed: {type(e).__name__}: {e}",
            ) from e

        return JsonResult(
            payload={
                "cql": req.query,
                "hits": [self._hit(s) for s in sections],
            }
        )

    @staticmethod
    def _hit(section: Section[str]) -> dict[str, str]:
        m = section.metadata
        return {
            "page_id": m.get(ConfluenceKeys.PAGE_ID) or "",
            "title": m.get(ReaderKeys.PAGE_TITLE) or "",
            "space_key": m.get(ConfluenceKeys.SPACE_KEY) or "",
            "url": str(section.source_id),
            "excerpt": section.content,
            "last_modified": m.get(HttpKeys.LAST_MODIFIED) or "",
        }

    @staticmethod
    def _build_query(query: str, space: str | None) -> str:
        text_search_block = f'text ~ "{quote(query, safe="")}"'
        res = text_search_block

        if space:
            res = f"({text_search_block}) and (space = {space})"

        return res
