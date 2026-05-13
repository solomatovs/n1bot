"""Tool: онлайн-чтение конкретной секции страницы Confluence по anchor."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, ClassVar

import httpx

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
from boba.tool.confluence.connection import (
    ConfluenceConnection,
)
from boba.tool.confluence.decoder import ConfluenceJsonDecoder
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

__all__ = [
    "ConfluencePageSectionTool",
    "ConfluencePageSectionToolConfig",
    "PageSectionArgs",
]


@dataclass(frozen=True)
class PageSectionArgs:
    """Читает текст одной секции страницы Confluence.

    От заголовка до следующего того же или большего уровня. page_id и anchor
    берутся из ответа confluence_page_outline.
    """

    page_id: Annotated[
        str,
        "ID страницы Confluence (как в confluence_page_outline).",
        NonEmpty(),
    ]
    anchor: Annotated[
        str,
        "Anchor нужного раздела (поле `anchor` из confluence_page_outline).",
        NonEmpty(),
    ]
    max_chars: Annotated[
        int,
        "Максимум символов в text-поле ответа (обрезка после).",
        MinValue(1),
        MaxValue(5000000),
    ]


@dataclass(frozen=True)
class ConfluencePageSectionToolConfig:
    """DTO tool'а: connection + body_format + prompt overlay."""

    base_url: str
    auth_method: str
    auth_user: str
    auth_token: str
    timeout_sec: float
    ssl_verify: bool
    body_format: str
    prompt: PromptOverlay


class ConfluencePageSectionTool(Tool[PageSectionArgs, ConfluencePageSectionToolConfig]):
    """Online-чтение одной секции страницы Confluence по anchor."""

    _PIPELINE_ID: ClassVar[PipelineId] = PipelineId("confluence.page_section")

    def execute(self, ctx: ToolContext, req: PageSectionArgs) -> ToolResult:
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
                message=f"Confluence page fetch failed: {type(e).__name__}: {e}",
            ) from e

        target = self._find_section(sections, req.anchor)
        if target is None:
            raise ToolExecutionError(
                tool_id=self.tool_id(),
                message=(
                    f"anchor={req.anchor!r} не найден на странице "
                    f"page_id={req.page_id!r}; вызовите confluence_page_outline "
                    f"для актуального списка."
                ),
            )

        text = target.content
        truncated = len(text) > req.max_chars
        if truncated:
            text = text[: req.max_chars - 1].rstrip() + "…"

        title = target.metadata.get(ReaderKeys.PAGE_TITLE) or ""
        return JsonResult(
            payload={
                "page_id": req.page_id,
                "anchor": req.anchor,
                "title": title,
                "url": str(target.source_id),
                "text": text,
                "truncated": truncated,
            }
        )

    @staticmethod
    def _find_section(
        sections: list[Section[str]],
        anchor: str,
    ) -> Section[str] | None:
        for s in sections:
            if s.metadata.get(SectionKeys.ANCHOR) == anchor:
                return s
        return None
