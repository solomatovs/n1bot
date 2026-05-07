"""Tool: онлайн-чтение конкретной секции страницы Confluence по anchor."""

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
from boba.ext.confluence_tools.parse import (
    anchor_for,
    collect_headings,
    parse_html,
    text_between,
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

__all__ = ["ConfluencePageSectionTool", "ConfluencePageSectionToolConfig"]


@dataclass(frozen=True)
class PageSectionArgs:
    page_id: str
    anchor: str
    max_chars: int


@dataclass(frozen=True)
class ConfluencePageSectionToolConfig:
    """DTO tool'а: connection + body_format + prompt overlay."""

    base_url: str
    auth_method: str
    auth_user: str
    auth_token: str
    timeout_sec: float
    body_format: str
    prompt: PromptOverlay


class ConfluencePageSectionTool(Tool[PageSectionArgs]):
    """Online-чтение одной секции страницы Confluence по anchor."""

    _NAME: ClassVar[ToolName] = ToolName("confluence_page_section")

    def __init__(
        self,
        cfg: ConfluencePageSectionToolConfig,
        ctx: ExtensionContext,
        source_id: ToolSourceId,
    ) -> None:
        self._cfg = cfg
        self._ctx = ctx
        self._tool_id = ToolId.compose(source_id, self._NAME)

    def tool_id(self) -> ToolId:
        return self._tool_id


    def definition(self) -> ObjectSchema[PageSectionArgs]:
        return self._cfg.prompt.apply(ObjectSchema(
            description=(
                "Читает текст одной секции страницы Confluence (от заголовка до "
                "следующего того же или большего уровня). page_id и anchor "
                "берутся из ответа confluence_page_outline."
            ),
            fields=[
                FieldSpec(
                    name="page_id",
                    description=(
                        "ID страницы Confluence (как в confluence_page_outline)."
                    ),
                    coercer=ChainCoercer(Required(), NonEmpty(), IsString()),
                ),
                FieldSpec(
                    name="anchor",
                    description=(
                        "Anchor нужного раздела (поле `anchor` из "
                        "confluence_page_outline)."
                    ),
                    coercer=ChainCoercer(Required(), NonEmpty(), IsString()),
                ),
                FieldSpec(
                    name="max_chars",
                    description="Максимум символов в text-поле ответа (обрезка после).",
                    coercer=ChainCoercer(Required(), IsInt(), MinValue(1), MaxValue(5000000)),
                ),
            ],
            factory=PageSectionArgs,
        ))

    def execute(self, ctx: ToolContext, req: PageSectionArgs) -> ToolResult:
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
                message=f"Confluence page fetch failed: {type(e).__name__}: {e}",
            ) from e

        text = self._extract_section_text(content.body_html, req.anchor)
        if text is None:
            raise ToolExecutionError(
                tool_id=self._tool_id,
                message=(
                    f"anchor={req.anchor!r} не найден на странице "
                    f"page_id={req.page_id!r}; вызовите confluence_page_outline "
                    f"для актуального списка."
                ),
            )

        truncated = len(text) > req.max_chars
        if truncated:
            text = text[: req.max_chars - 1].rstrip() + "…"

        return JsonResult(
            payload={
                "page_id": content.page_id,
                "anchor": req.anchor,
                "title": content.title,
                "url": content.url,
                "text": text,
                "truncated": truncated,
            }
        )

    @staticmethod
    def _extract_section_text(html: str, anchor: str) -> str | None:
        if not html:
            return None
        soup = parse_html(html)
        headings = collect_headings(soup)
        target_idx: int | None = None
        for i, h in enumerate(headings):
            if anchor_for(h) == anchor:
                target_idx = i
                break
        if target_idx is None:
            return None
        target = headings[target_idx]
        stop = next(
            (h for h in headings[target_idx + 1 :] if h.level <= target.level),
            None,
        )
        between = text_between(target.tag, stop.tag if stop else None)
        head = target.text.strip()
        return f"{head}\n\n{between}".rstrip() if between else head
