"""Tool: онлайн-outline страницы Confluence по page_id."""

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
from boba.declaration import FieldSpec, ObjectSchema
from boba.ext.confluence_tools._http_client import ConfluenceHttpClient
from boba.ext.confluence_tools._page_pipeline import (
    ConfluencePagePipeline,
    ConfluencePagePipelineError,
)
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
    "ConfluencePageOutlineTool",
    "ConfluencePageOutlineToolConfig",
    "ConfluencePageOutlineToolSection",
]


@dataclass(frozen=True)
class PageOutlineArgs:
    page_id: str
    max_headings: int


@dataclass(frozen=True)
class ConfluencePageOutlineToolConfig:
    description: str
    params: Mapping[str, ParamOverlay] = field(default_factory=dict)


class ConfluencePageOutlineTool(Tool[PageOutlineArgs]):
    """Online-outline страницы Confluence: page_id → структура заголовков."""

    _ID = ToolId("confluence_page_outline")
    _SOURCE = ToolSourceId("builtin.confluence")

    DEFAULT_DESCRIPTION: ClassVar[str] = (
        "Получает структуру заголовков (h1..h6) страницы Confluence по page_id. "
        "Возвращает title, метаданные и список секций с anchor'ами для "
        "последующего вызова confluence_page_section."
    )
    DEFAULT_PAGE_ID_DESC: ClassVar[str] = (
        "ID страницы Confluence (число; виден в URL viewpage.action?pageId=...)."
    )
    DEFAULT_MAX_HEADINGS_DESC: ClassVar[str] = (
        "Максимум заголовков в ответе (защита от длинных страниц)."
    )

    def __init__(
        self,
        tool_cfg: ConfluencePageOutlineToolConfig,
        runtime_cfg: ConfluencePageConfig,
    ) -> None:
        self._cfg = tool_cfg
        self._runtime = runtime_cfg

    def tool_id(self) -> ToolId:
        return self._ID

    def tool_source_id(self) -> ToolSourceId:
        return self._SOURCE

    def definition(self) -> ObjectSchema[PageOutlineArgs]:
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
                    name="max_headings",
                    description=param_desc(
                        p, "max_headings", self.DEFAULT_MAX_HEADINGS_DESC,
                    ),
                    coercer=ChainCoercer(IsInt(), MinValue(1), MaxValue(500)),
                    required=True,
                ),
            ],
            factory=PageOutlineArgs,
        )

    def execute(self, ctx: ToolContext, req: PageOutlineArgs) -> ToolResult:
        del ctx
        try:
            with ConfluenceHttpClient.make(self._runtime) as c:
                pipeline = ConfluencePagePipeline(
                    client=c,
                    base_url=self._runtime.base_url,
                    body_format=self._runtime.body_format,
                )
                content = pipeline.fetch(req.page_id)
        except ConfluencePagePipelineError as e:
            raise ToolExecutionError(
                tool_id=self._ID,
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


class ConfluencePageOutlineToolSection(
    ConfigSection[ConfluencePageOutlineToolConfig],
):
    """Секция [ext.confluence.tools.confluence_page_outline]."""

    namespace: ClassVar[tuple[str, ...]] = (
        "ext", "confluence", "tools", "confluence_page_outline",
    )

    schema: ClassVar[ObjectSchema[ConfluencePageOutlineToolConfig]] = ObjectSchema(
        description="Конфиг tool 'confluence_page_outline'.",
        fields=[
            FieldSpec(
                name="description",
                coercer=ChainCoercer(
                    Default(ConfluencePageOutlineTool.DEFAULT_DESCRIPTION),
                    ParseString(),
                ),
                description="Override описания tool'а; пусто — дефолт из кода.",
            ),
            params_field("params"),
        ],
        factory=ConfluencePageOutlineToolConfig,
    )
