"""Tool: оглавление HTML-документа из workspace."""

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
    Nullable,
    ParseString,
)
from boba.config.section import ConfigSection
from boba.declaration import FieldSpec, ObjectSchema
from boba.ext.html_tools._parse import Heading, anchor_for, collect_headings, load_soup
from boba.tools.domain import (
    ParamOverlay,
    TextResult,
    Tool,
    ToolContext,
    ToolExecutionError,
    ToolId,
    ToolResult,
    ToolSourceId,
    param_desc,
    params_field,
)
from boba.workspace import (
    WorkspaceError,
    WorkspaceNotFoundError,
)


@dataclass(frozen=True)
class OutlineArgs:
    path: str
    max_depth: int | None
    limit: int


@dataclass(frozen=True)
class HtmlOutlineToolConfig:
    """DTO секции [ext.html.tools.html_outline]."""

    description: str
    params: Mapping[str, ParamOverlay] = field(default_factory=dict)


class HtmlOutlineTool(Tool[OutlineArgs]):
    """Иерархия <h1>..<h6> HTML-документа с anchor'ами для html_section."""

    _ID = ToolId("html_outline")
    _SOURCE = ToolSourceId("builtin.html")

    DEFAULT_DESCRIPTION: ClassVar[str] = (
        "Оглавление HTML-файла: иерархия <h1>..<h6> с anchor'ами. "
        "Anchor — либо #<id> атрибута заголовка, либо #idx:N "
        "(порядковый номер). Используется как вход в html_section."
    )
    DEFAULT_PATH_DESC: ClassVar[str] = "Путь к HTML-файлу в workspace."
    DEFAULT_MAX_DEPTH_DESC: ClassVar[str] = (
        "Максимальный уровень заголовков (1=h1..6=h6). Без значения — все 6."
    )
    DEFAULT_LIMIT_DESC: ClassVar[str] = "Максимум заголовков в ответе."

    def __init__(self, cfg: HtmlOutlineToolConfig) -> None:
        self._cfg = cfg

    def tool_id(self) -> ToolId:
        return self._ID

    def tool_source_id(self) -> ToolSourceId:
        return self._SOURCE

    def definition(self) -> ObjectSchema[OutlineArgs]:
        p = self._cfg.params
        return ObjectSchema(
            description=self._cfg.description,
            fields=[
                FieldSpec(
                    name="path",
                    description=param_desc(p, "path", self.DEFAULT_PATH_DESC),
                    coercer=ChainCoercer(IsString(), NonEmpty()),
                    required=True,
                ),
                FieldSpec(
                    name="max_depth",
                    description=param_desc(p, "max_depth", self.DEFAULT_MAX_DEPTH_DESC),
                    coercer=Nullable(
                        ChainCoercer(IsInt(), MinValue(1), MaxValue(6))
                    ),
                ),
                FieldSpec(
                    name="limit",
                    description=param_desc(p, "limit", self.DEFAULT_LIMIT_DESC),
                    coercer=ChainCoercer(Default(200), IsInt(), MinValue(1)),
                ),
            ],
            factory=OutlineArgs,
        )

    def execute(self, ctx: ToolContext, req: OutlineArgs) -> ToolResult:
        try:
            soup = load_soup(ctx.project_workspace, req.path)
        except WorkspaceNotFoundError as e:
            raise ToolExecutionError(
                tool_id=self._ID, message=f"Файл не найден: {req.path}"
            ) from e
        except WorkspaceError as e:
            raise ToolExecutionError(
                tool_id=self._ID, message=f"Ошибка чтения: {e}"
            ) from e

        headings = collect_headings(soup, max_depth=req.max_depth)
        truncated = len(headings) > req.limit
        if truncated:
            headings = headings[: req.limit]

        title = soup.title.get_text(strip=True) if soup.title else ""
        charset = soup.original_encoding or ""

        head = f"Документ: {req.path}"
        if title:
            head += f"  title={title!r}"
        if charset:
            head += f"  charset={charset}"

        if not headings:
            return TextResult(text=f"{head}\nЗаголовков: 0")

        body = "\n".join(_render_line(h) for h in headings)
        suffix = f", truncated at limit={req.limit}" if truncated else ""
        return TextResult(text=f"{head}\nЗаголовков: {len(headings)}{suffix}\n\n{body}",
        )


def _render_line(h: Heading) -> str:
    indent = "  " * (h.level - 1)
    return f"{h.index:>3}. {indent}h{h.level} {h.text}  #{anchor_for(h)}"


class HtmlOutlineToolSection(ConfigSection[HtmlOutlineToolConfig]):
    """Секция [ext.html.tools.html_outline]."""

    namespace: ClassVar[tuple[str, ...]] = (
        "ext",
        "html",
        "tools",
        "html_outline",
    )

    schema: ClassVar[ObjectSchema[HtmlOutlineToolConfig]] = ObjectSchema(
        description="Конфиг tool 'html_outline'.",
        fields=[
            FieldSpec(
                name="description",
                coercer=ChainCoercer(
                    Default(HtmlOutlineTool.DEFAULT_DESCRIPTION), ParseString()
                ),
                description="Override описания tool'а; пусто — дефолт из кода.",
            ),
            params_field("params"),
        ],
        factory=HtmlOutlineToolConfig,
    )
