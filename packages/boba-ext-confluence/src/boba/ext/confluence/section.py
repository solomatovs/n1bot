"""Tool: содержимое раздела Confluence-export'а по anchor."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from bs4.element import Tag

from boba.domain.core.tools import (
    ChainConverter,
    Default,
    FieldSpec,
    IsBool,
    IsInt,
    IsString,
    MinValue,
    NonEmpty,
    ObjectSchema,
    Pass,
    Required,
    Tool,
    ToolContext,
    ToolExecutionError,
    ToolId,
    ToolResult,
    ToolSourceId,
)
from boba.domain.core.workspace import (
    WorkspaceError,
    WorkspaceNotFoundError,
)
from boba.ext.confluence._parse import (
    Heading,
    collect_headings,
    load_soup,
    resolve_anchor,
    strip_confluence_macros,
)
from boba.patterns import Converter


@dataclass(frozen=True)
class SectionArgs:
    path: str
    anchor: str
    include_subsections: bool
    strip_macros: bool
    max_chars: int


class SectionArgsConverter(Converter[dict[str, Any], SectionArgs]):
    def convert(self, value: dict[str, Any]) -> SectionArgs:
        return SectionArgs(
            path=value["path"],
            anchor=value["anchor"],
            include_subsections=value["include_subsections"],
            strip_macros=value["strip_macros"],
            max_chars=value["max_chars"],
        )


class ConfluenceSectionTool(Tool[SectionArgs]):
    """HTML фрагмент раздела Confluence-страницы от заголовка до следующего."""

    _ID = ToolId("confluence_section")
    _SOURCE = ToolSourceId("builtin.confluence")

    def tool_id(self) -> ToolId:
        return self._ID

    def tool_source_id(self) -> ToolSourceId:
        return self._SOURCE

    def typed_args_converter(self) -> Converter[dict[str, Any], SectionArgs]:
        return SectionArgsConverter()

    def definition(self) -> ObjectSchema[dict[str, Any]]:
        return ObjectSchema(
            description=(
                "Раздел Confluence-страницы по anchor (scroll-bookmark-N или idx:N "
                "из confluence_outline; ведущий # необязателен). По умолчанию "
                "вырезает ac:*/ri:* макросы — модели достаётся чистый HTML."
            ),
            fields=[
                FieldSpec(
                    name="path",
                    description="Путь к HTML-файлу Confluence-export'а в workspace.",
                    converter=ChainConverter(Required(), IsString(), NonEmpty()),
                ),
                FieldSpec(
                    name="anchor",
                    description=(
                        "Anchor заголовка из confluence_outline "
                        "(scroll-bookmark-N или idx:N). Ведущий '#' необязателен."
                    ),
                    converter=ChainConverter(Required(), IsString(), NonEmpty()),
                ),
                FieldSpec(
                    name="include_subsections",
                    description=(
                        "true — включать вложенные подзаголовки (стоп на "
                        "следующем заголовке того же или меньшего уровня); "
                        "false — стоп на любом следующем заголовке."
                    ),
                    converter=ChainConverter(Default(True), IsBool()),
                ),
                FieldSpec(
                    name="strip_macros",
                    description=(
                        "true (default) — вырезать confluence-макросы "
                        "(ac:*/ri:*) из ответа; false — отдать HTML как есть."
                    ),
                    converter=ChainConverter(Default(True), IsBool()),
                ),
                FieldSpec(
                    name="max_chars",
                    description="Лимит длины ответа в символах.",
                    converter=ChainConverter(
                        Default(8000), IsInt(), MinValue(100)
                    ),
                ),
            ],
            invariants=Pass(),
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
                    "получи актуальные anchor'ы через confluence_outline."
                ),
            )

        target_idx = headings.index(target)
        stop = _find_stop_heading(headings, target_idx, req.include_subsections)
        html = _collect_section_html(target.tag, stop.tag if stop else None)

        if req.strip_macros:
            html = strip_confluence_macros(html)

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
    """Sibling'и start (включая) до stop или конца parent'а."""
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
