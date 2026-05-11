"""Tool: онлайн-outline страницы Confluence по page_id."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Any, ClassVar

import httpx

from boba.html import HtmlKeys
from boba.indexing import (
    PipelineContext,
    PipelineId,
    ReaderKeys,
    RuntimePipeline,
    Section,
    SectionKeys,
)
from boba.plugin.prompt import PromptOverlay
from boba.schema.coercion import MaxValue, MinValue, NonEmpty
from boba.tool.confluence.connection import ConfluenceConnection
from boba.tool.confluence.decoder import ConfluenceJsonDecoder
from boba.tool.confluence.keys import ConfluenceKeys
from boba.tool.confluence.reader import ConfluenceReader
from boba.tool.confluence.request_sources.pages import (
    ConfluencePagesRequestSource,
)
from boba.tools.domain import (
    JsonResult,
    Tool,
    ToolContext,
    ToolExecutionError,
    ToolResult,
)
from boba.transport.http import HttpKeys

__all__ = [
    "ConfluencePageOutlineTool",
    "ConfluencePageOutlineToolConfig",
    "PageOutlineArgs",
]


@dataclass(frozen=True)
class PageOutlineArgs:
    """Получает структуру заголовков (h1..h6) страницы Confluence по page_id.

    Возвращает title, метаданные и список секций с anchor'ами для последующего
    вызова confluence_page_section.
    """

    page_id: Annotated[
        str,
        "ID страницы Confluence (число; виден в URL "
        "viewpage.action?pageId=...).",
        NonEmpty(),
    ]
    max_headings: Annotated[
        int,
        "Максимум заголовков в ответе (защита от длинных страниц).",
        MinValue(1),
        MaxValue(500),
    ]


@dataclass(frozen=True)
class ConfluencePageOutlineToolConfig:
    """DTO tool'а: connection + body_format + prompt overlay."""

    base_url: str
    auth_method: str
    auth_user: str
    auth_token: str
    timeout_sec: float
    body_format: str
    prompt: PromptOverlay


class ConfluencePageOutlineTool(
    Tool[PageOutlineArgs, ConfluencePageOutlineToolConfig]
):
    """Online-outline страницы Confluence: page_id → структура заголовков."""

    _PIPELINE_ID: ClassVar[PipelineId] = PipelineId("confluence.page_outline")

    def execute(self, ctx: ToolContext, req: PageOutlineArgs) -> ToolResult:
        del ctx
        pipeline: RuntimePipeline = RuntimePipeline(
            request_source=ConfluencePagesRequestSource(
                base_url=self._cfg.base_url,
                auth=ConfluenceConnection.make_auth(self._cfg),
                page_ids=[req.page_id],
                body_format=self._cfg.body_format,
            ),
            transport=ConfluenceConnection.make_transport(self._cfg),
            decoder=ConfluenceJsonDecoder(body_format=self._cfg.body_format),
            reader=ConfluenceReader(),
        )

        try:
            sections = list(
                pipeline.stream(PipelineContext(pipeline_id=self._PIPELINE_ID))
            )
        except httpx.HTTPError as e:
            raise ToolExecutionError(
                tool_id=self.tool_id(),
                message=f"Confluence page outline failed: {type(e).__name__}: {e}",
            ) from e

        headings = [s for s in sections if s.metadata.has(HtmlKeys.HEADING_LEVEL)]
        total = len(headings)
        truncated = total > req.max_headings
        meta = self._page_meta(sections, req.page_id)

        return JsonResult(payload={
            "page_id": req.page_id,
            "title": meta["title"],
            "space_key": meta["space_key"],
            "url": meta["url"],
            "version": meta["version"],
            "last_modified": meta["last_modified"],
            "sections": [
                {
                    "level": h.metadata.get(HtmlKeys.HEADING_LEVEL) or 0,
                    "text": h.metadata.get(HtmlKeys.HEADING_TEXT) or "",
                    "anchor": h.metadata.get(SectionKeys.ANCHOR) or "",
                }
                for h in headings[: req.max_headings]
            ],
            "truncated": truncated,
            "total_headings": total,
        })

    @staticmethod
    def _page_meta(sections: list[Section[str]], page_id: str) -> dict[str, Any]:
        """Page-level метаданные. Все секции одной страницы делят metadata."""
        if not sections:
            return {
                "title": "",
                "space_key": "",
                "url": "",
                "version": "",
                "last_modified": "",
            }
        first = sections[0]
        m = first.metadata
        version = m.get(ConfluenceKeys.VERSION)
        return {
            "title": m.get(ReaderKeys.PAGE_TITLE) or "",
            "space_key": m.get(ConfluenceKeys.SPACE_KEY) or "",
            "url": str(first.source_id),
            "version": str(version) if version is not None else "",
            "last_modified": m.get(HttpKeys.LAST_MODIFIED) or "",
        }
