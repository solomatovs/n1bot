"""Tool: онлайн-outline страницы Confluence по page_id."""

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
from boba.ext.confluence_tools._page_pipeline import (
    ConfluencePagePipeline,
    ConfluencePagePipelineError,
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

__all__ = ["ConfluencePageOutlineTool", "ConfluencePageOutlineToolConfig"]


@dataclass(frozen=True)
class PageOutlineArgs:
    page_id: str
    max_headings: int


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


class ConfluencePageOutlineTool(Tool[PageOutlineArgs]):
    """Online-outline страницы Confluence: page_id → структура заголовков."""

    _NAME: ClassVar[ToolName] = ToolName("confluence_page_outline")

    def __init__(
        self,
        cfg: ConfluencePageOutlineToolConfig,
        ctx: ExtensionContext,
        source_id: ToolSourceId,
    ) -> None:
        self._cfg = cfg
        self._ctx = ctx
        self._tool_id = ToolId.compose(source_id, self._NAME)

    def tool_id(self) -> ToolId:
        return self._tool_id


    def definition(self) -> ObjectSchema[PageOutlineArgs]:
        return self._cfg.prompt.apply(ObjectSchema(
            description=(
                "Получает структуру заголовков (h1..h6) страницы Confluence по "
                "page_id. Возвращает title, метаданные и список секций с "
                "anchor'ами для последующего вызова confluence_page_section."
            ),
            fields=[
                FieldSpec(
                    name="page_id",
                    description=(
                        "ID страницы Confluence (число; виден в URL "
                        "viewpage.action?pageId=...)."
                    ),
                    coercer=ChainCoercer(Required(), NonEmpty(), IsString()),
                ),
                FieldSpec(
                    name="max_headings",
                    description=(
                        "Максимум заголовков в ответе (защита от длинных страниц)."
                    ),
                    coercer=ChainCoercer(Required(), IsInt(), MinValue(1), MaxValue(500)),
                ),
            ],
            factory=PageOutlineArgs,
        ))

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
                tool_id=self._tool_id,
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
