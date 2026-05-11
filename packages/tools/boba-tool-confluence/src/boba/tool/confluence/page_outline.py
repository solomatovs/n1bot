"""Tool: онлайн-outline страницы Confluence по page_id."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated

from boba.plugin.prompt import PromptOverlay
from boba.schema.coercion import MaxValue, MinValue, NonEmpty
from boba.tool.confluence._http_client import ConfluenceHttpClient
from boba.tool.confluence._page_pipeline import (
    ConfluencePagePipeline,
    ConfluencePagePipelineError,
)
from boba.tools.domain import (
    JsonResult,
    Tool,
    ToolContext,
    ToolExecutionError,
    ToolResult,
)

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

    def execute(self, ctx: ToolContext, req: PageOutlineArgs) -> ToolResult:
        del ctx
        try:
            with ConfluenceHttpClient.make(self._cfg) as c:
                pipeline = ConfluencePagePipeline(
                    client=c,
                    base_url=self._cfg.base_url,
                    body_format=self._cfg.body_format,
                )
                content = pipeline.fetch(req.page_id)
        except ConfluencePagePipelineError as e:
            raise ToolExecutionError(
                tool_id=self.tool_id(),
                message=f"Confluence page outline failed: {type(e).__name__}: {e}",
            ) from e

        headings = content.headings[: req.max_headings]
        truncated = len(content.headings) > req.max_headings
        return JsonResult(payload={
            "page_id": content.page_id,
            "title": content.title,
            "space_key": content.space_key,
            "url": content.url,
            "version": content.version,
            "last_modified": content.last_modified,
            "sections": [
                {"level": h.level, "text": h.text, "anchor": h.anchor}
                for h in headings
            ],
            "truncated": truncated,
            "total_headings": len(content.headings),
        })
