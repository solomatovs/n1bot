"""Tool: содержимое раздела HTML по anchor."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import ClassVar

from bs4.element import Tag

from boba.coercion import (
    ChainCoercer,
    Default,
    IsBool,
    IsInt,
    IsString,
    MinValue,
    NonEmpty,
    ParseString,
)
from boba.config.section import ConfigSection
from boba.declaration import FieldSpec, ObjectSchema
from boba.ext.html_tools._parse import (
    Heading,
    collect_headings,
    load_soup,
    resolve_anchor,
)
from boba.tools import (
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
from boba.workspace import (
    WorkspaceError,
    WorkspaceNotFoundError,
)


@dataclass(frozen=True)
class SectionArgs:
    path: str
    anchor: str
    include_subsections: bool
    max_chars: int


@dataclass(frozen=True)
class HtmlSectionToolConfig:
    """DTO секции [ext.html.tools.html_section]."""

    description: str
    params: Mapping[str, ParamOverlay] = field(default_factory=dict)


class HtmlSectionTool(Tool[SectionArgs]):
    """HTML фрагмент раздела от заголовка до следующего заголовка."""

    _ID = ToolId("html_section")
    _SOURCE = ToolSourceId("builtin.html")

    DEFAULT_DESCRIPTION: ClassVar[str] = (
        "Вернуть HTML-фрагмент раздела от выбранного заголовка до "
        "следующего. anchor берётся из html_outline (idx:N или html-id; "
        "ведущий # необязателен). Содержимое возвращается как есть, "
        "без преобразований в markdown/текст."
    )
    DEFAULT_PATH_DESC: ClassVar[str] = "Путь к HTML-файлу в workspace."
    DEFAULT_ANCHOR_DESC: ClassVar[str] = (
        "Anchor заголовка из html_outline (idx:N или html id). "
        "Ведущий '#' необязателен."
    )
    DEFAULT_INCLUDE_SUBSECTIONS_DESC: ClassVar[str] = (
        "true — включать вложенные подзаголовки (стоп на "
        "следующем заголовке того же или меньшего уровня); "
        "false — стоп на любом следующем заголовке."
    )
    DEFAULT_MAX_CHARS_DESC: ClassVar[str] = "Лимит длины ответа в символах."

    def __init__(self, cfg: HtmlSectionToolConfig) -> None:
        self._cfg = cfg

    def tool_id(self) -> ToolId:
        return self._ID

    def tool_source_id(self) -> ToolSourceId:
        return self._SOURCE

    def definition(self) -> ObjectSchema[SectionArgs]:
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
                    name="anchor",
                    description=param_desc(
                        p, "anchor", self.DEFAULT_ANCHOR_DESC
                    ),
                    coercer=ChainCoercer(IsString(), NonEmpty()),
                    required=True,
                ),
                FieldSpec(
                    name="include_subsections",
                    description=param_desc(
                        p,
                        "include_subsections",
                        self.DEFAULT_INCLUDE_SUBSECTIONS_DESC,
                    ),
                    coercer=ChainCoercer(Default(True), IsBool()),
                ),
                FieldSpec(
                    name="max_chars",
                    description=param_desc(
                        p, "max_chars", self.DEFAULT_MAX_CHARS_DESC
                    ),
                    coercer=ChainCoercer(Default(8000), IsInt(), MinValue(100)),
                ),
            ],
            factory=SectionArgs,
        )

    def execute(self, ctx: ToolContext, req: SectionArgs) -> ToolResult:
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

        headings = collect_headings(soup)
        target = resolve_anchor(headings, req.anchor)
        if target is None:
            raise ToolExecutionError(
                tool_id=self._ID,
                message=(
                    f"Заголовок с anchor={req.anchor!r} не найден; "
                    "получи актуальные anchor'ы через html_outline."
                ),
            )

        target_idx = headings.index(target)
        stop = _find_stop_heading(headings, target_idx, req.include_subsections)
        html = _collect_section_html(target.tag, stop.tag if stop else None)

        if len(html) > req.max_chars:
            html = (
                html[: req.max_chars]
                + f"\n... (truncated at max_chars={req.max_chars})"
            )

        return ToolResult(content=html)


def _find_stop_heading(
    headings: list[Heading],
    target_idx: int,
    include_subsections: bool,
) -> Heading | None:
    target = headings[target_idx]
    for h in headings[target_idx + 1 :]:
        if include_subsections:
            if h.level <= target.level:
                return h
        else:
            return h
    return None


def _collect_section_html(start: Tag, stop: Tag | None) -> str:
    """Sibling'и start (включая) до stop или конца parent'а; stop в descendants — обрезаем."""
    parts: list[str] = [str(start)]
    for sib in start.next_siblings:
        if stop is not None and sib is stop:
            break
        if (
            stop is not None
            and isinstance(sib, Tag)
            and any(d is stop for d in sib.descendants)
        ):
            break
        parts.append(str(sib))
    return "".join(parts)


class HtmlSectionToolSection(ConfigSection[HtmlSectionToolConfig]):
    """Секция [ext.html.tools.html_section]."""

    namespace: ClassVar[tuple[str, ...]] = (
        "ext", "html", "tools", "html_section",
    )

    schema: ClassVar[ObjectSchema[HtmlSectionToolConfig]] = ObjectSchema(
        description="Конфиг tool 'html_section'.",
        fields=[
            FieldSpec(
                name="description",
                coercer=ChainCoercer(
                    Default(HtmlSectionTool.DEFAULT_DESCRIPTION), ParseString()
                ),
                description="Override описания tool'а; пусто — дефолт из кода.",
            ),
            params_field("params"),
        ],
        factory=HtmlSectionToolConfig,
    )
