"""Tool: содержимое раздела HTML по anchor."""

from __future__ import annotations

from dataclasses import dataclass
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
    Required,
)
from boba.declaration import FieldSpec, ObjectSchema
from boba.ext.html_tools._parse import (
    Heading,
    collect_headings,
    load_soup,
    resolve_anchor,
)
from boba.plugin import ExtensionContext
from boba.plugin.prompt import PromptOverlay
from boba.tools.domain import (
    TextResult,
    Tool,
    ToolContext,
    ToolExecutionError,
    ToolId,
    ToolName,
    ToolSourceId,
    ToolResult,
)
from boba.workspace import (
    WorkspaceError,
    WorkspaceNotFoundError,
)

__all__ = ["HtmlSectionTool", "HtmlSectionToolConfig"]


@dataclass(frozen=True)
class SectionArgs:
    path: str
    anchor: str
    include_subsections: bool
    max_chars: int


@dataclass(frozen=True)
class HtmlSectionToolConfig:
    """DTO tool'а: только prompt overlay."""

    prompt: PromptOverlay


class HtmlSectionTool(Tool[SectionArgs]):
    """HTML фрагмент раздела от заголовка до следующего заголовка."""

    _NAME: ClassVar[ToolName] = ToolName("html_section")

    def __init__(self, cfg: HtmlSectionToolConfig, ctx: ExtensionContext, source_id: ToolSourceId) -> None:
        self._cfg = cfg
        self._ctx = ctx
        self._tool_id = ToolId.compose(source_id, self._NAME)

    def tool_id(self) -> ToolId:
        return self._tool_id


    def definition(self) -> ObjectSchema[SectionArgs]:
        return self._cfg.prompt.apply(ObjectSchema(
            description=(
                "Вернуть HTML-фрагмент раздела от выбранного заголовка до "
                "следующего. anchor берётся из html_outline (idx:N или html-id; "
                "ведущий # необязателен). Содержимое возвращается как есть, "
                "без преобразований в markdown/текст."
            ),
            fields=[
                FieldSpec(
                    name="path",
                    description="Путь к HTML-файлу в workspace.",
                    coercer=ChainCoercer(Required(), IsString(), NonEmpty()),
                ),
                FieldSpec(
                    name="anchor",
                    description=(
                        "Anchor заголовка из html_outline (idx:N или html id). "
                        "Ведущий '#' необязателен."
                    ),
                    coercer=ChainCoercer(Required(), IsString(), NonEmpty()),
                ),
                FieldSpec(
                    name="include_subsections",
                    description=(
                        "true — включать вложенные подзаголовки (стоп на "
                        "следующем заголовке того же или меньшего уровня); "
                        "false — стоп на любом следующем заголовке."
                    ),
                    coercer=ChainCoercer(Default(True), IsBool()),
                ),
                FieldSpec(
                    name="max_chars",
                    description="Лимит длины ответа в символах.",
                    coercer=ChainCoercer(Default(8000), IsInt(), MinValue(100)),
                ),
            ],
            factory=SectionArgs,
        ))

    def execute(self, ctx: ToolContext, req: SectionArgs) -> ToolResult:
        try:
            soup = load_soup(ctx.project_workspace, req.path)
        except WorkspaceNotFoundError as e:
            raise ToolExecutionError(
                tool_id=self._tool_id, message=f"Файл не найден: {req.path}"
            ) from e
        except WorkspaceError as e:
            raise ToolExecutionError(
                tool_id=self._tool_id, message=f"Ошибка чтения: {e}"
            ) from e

        headings = collect_headings(soup)
        target = resolve_anchor(headings, req.anchor)
        if target is None:
            raise ToolExecutionError(
                tool_id=self._tool_id,
                message=(
                    f"Заголовок с anchor={req.anchor!r} не найден; "
                    "получи актуальные anchor'ы через html_outline."
                ),
            )

        target_idx = headings.index(target)
        stop = self._find_stop_heading(headings, target_idx, req.include_subsections)
        html = self._collect_section_html(target.tag, stop.tag if stop else None)

        if len(html) > req.max_chars:
            html = (
                html[: req.max_chars]
                + f"\n... (truncated at max_chars={req.max_chars})"
            )

        return TextResult(text=html)

    @staticmethod
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

    @staticmethod
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
