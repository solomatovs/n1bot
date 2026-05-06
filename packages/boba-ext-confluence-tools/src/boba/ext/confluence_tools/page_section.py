"""Tool: онлайн-чтение конкретной секции страницы Confluence по anchor."""

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
from boba.confluence_page_pipeline import (
    ConfluencePagePipeline,
    ConfluencePagePipelineError,
)
from boba.confluence_shared import (
    ConfluenceConnection,
    anchor_for,
    collect_headings,
    parse_html,
    text_between,
)
from boba.declaration import FieldSpec, ObjectSchema
from boba.ext.confluence_tools.page import ConfluencePageConfig
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
    "ConfluencePageSectionTool",
    "ConfluencePageSectionToolConfig",
    "ConfluencePageSectionToolSection",
]


@dataclass(frozen=True)
class PageSectionArgs:
    page_id: str
    anchor: str
    max_chars: int


@dataclass(frozen=True)
class ConfluencePageSectionToolConfig:
    description: str
    params: Mapping[str, ParamOverlay] = field(default_factory=dict)


class ConfluencePageSectionTool(Tool[PageSectionArgs]):
    """Online-чтение одной секции страницы Confluence по anchor."""

    _ID = ToolId("confluence_page_section")
    _SOURCE = ToolSourceId("builtin.confluence")

    DEFAULT_DESCRIPTION: ClassVar[str] = (
        "Читает текст одной секции страницы Confluence (от заголовка до "
        "следующего того же или большего уровня). page_id и anchor берутся "
        "из ответа confluence_page_outline."
    )
    DEFAULT_PAGE_ID_DESC: ClassVar[str] = (
        "ID страницы Confluence (как в confluence_page_outline)."
    )
    DEFAULT_ANCHOR_DESC: ClassVar[str] = (
        "Anchor нужного раздела (поле `anchor` из confluence_page_outline)."
    )
    DEFAULT_MAX_CHARS_DESC: ClassVar[str] = (
        "Максимум символов в text-поле ответа (обрезка после)."
    )

    def __init__(
        self,
        tool_cfg: ConfluencePageSectionToolConfig,
        runtime_cfg: ConfluencePageConfig,
    ) -> None:
        self._cfg = tool_cfg
        self._runtime = runtime_cfg

    def tool_id(self) -> ToolId:
        return self._ID

    def tool_source_id(self) -> ToolSourceId:
        return self._SOURCE

    def definition(self) -> ObjectSchema[PageSectionArgs]:
        p = self._cfg.params
        return ObjectSchema(
            description=self._cfg.description,
            fields=[
                FieldSpec(
                    name="page_id",
                    description=param_desc(p, "page_id", self.DEFAULT_PAGE_ID_DESC),
                    coercer=ChainCoercer(NonEmpty(), IsString()),
                    required=True,
                ),
                FieldSpec(
                    name="anchor",
                    description=param_desc(p, "anchor", self.DEFAULT_ANCHOR_DESC),
                    coercer=ChainCoercer(NonEmpty(), IsString()),
                    required=True,
                ),
                FieldSpec(
                    name="max_chars",
                    description=param_desc(
                        p, "max_chars", self.DEFAULT_MAX_CHARS_DESC,
                    ),
                    coercer=ChainCoercer(IsInt(), MinValue(1), MaxValue(5000000)),
                    required=True,
                ),
            ],
            factory=PageSectionArgs,
        )

    def execute(self, ctx: ToolContext, req: PageSectionArgs) -> ToolResult:
        del ctx
        try:
            with ConfluenceConnection.make_discovery_client(self._runtime) as c:
                pipeline = ConfluencePagePipeline(
                    client=c,
                    base_url=self._runtime.base_url,
                    body_format=self._runtime.body_format,
                )
                content = pipeline.fetch(req.page_id)
        except ConfluencePagePipelineError as e:
            raise ToolExecutionError(
                tool_id=self._ID,
                message=f"Confluence page fetch failed: {type(e).__name__}: {e}",
            ) from e

        text = self._extract_section_text(content.body_html, req.anchor)
        if text is None:
            raise ToolExecutionError(
                tool_id=self._ID,
                message=(
                    f"anchor={req.anchor!r} не найден на странице "
                    f"page_id={req.page_id!r}; вызовите confluence_page_outline "
                    f"для актуального списка."
                ),
            )

        truncated = len(text) > req.max_chars
        if truncated:
            text = text[: req.max_chars - 1].rstrip() + "…"

        return JsonResult(payload={
            "page_id": content.page_id,
            "anchor": req.anchor,
            "title": content.title,
            "url": content.url,
            "text": text,
            "truncated": truncated,
        })

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
            (
                h for h in headings[target_idx + 1:]
                if h.level <= target.level
            ),
            None,
        )
        between = text_between(target.tag, stop.tag if stop else None)
        head = target.text.strip()
        return f"{head}\n\n{between}".rstrip() if between else head


class ConfluencePageSectionToolSection(
    ConfigSection[ConfluencePageSectionToolConfig],
):
    """Секция [ext.confluence.tools.confluence_page_section]."""

    namespace: ClassVar[tuple[str, ...]] = (
        "ext", "confluence", "tools", "confluence_page_section",
    )

    schema: ClassVar[ObjectSchema[ConfluencePageSectionToolConfig]] = ObjectSchema(
        description="Конфиг tool 'confluence_page_section'.",
        fields=[
            FieldSpec(
                name="description",
                coercer=ChainCoercer(
                    Default(ConfluencePageSectionTool.DEFAULT_DESCRIPTION),
                    ParseString(),
                ),
                description="Override описания tool'а; пусто — дефолт из кода.",
            ),
            params_field("params"),
        ],
        factory=ConfluencePageSectionToolConfig,
    )
