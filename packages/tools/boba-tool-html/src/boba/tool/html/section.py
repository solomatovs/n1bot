"""Tool: содержимое раздела HTML по anchor."""

from __future__ import annotations

from dataclasses import dataclass

from bs4.element import Tag
from pydantic import BaseModel, ConfigDict, Field

from boba.plugin.prompt import PromptOverlay
from boba.tool.html._base import HtmlToolBase
from boba.tool.html._parse import (
    Heading,
    collect_headings,
    load_soup,
    resolve_anchor,
)
from boba.tools.domain import (
    JsonResult,
    ToolContext,
    ToolExecutionError,
    ToolResult,
)
from boba.workspace.contract import (
    WorkspaceError,
    WorkspaceNotFoundError,
)

__all__ = ["HtmlSectionTool", "HtmlSectionToolConfig", "SectionArgs"]


class SectionArgs(BaseModel):
    """Вернуть HTML-фрагмент раздела от выбранного заголовка до следующего.

    anchor берётся из html_outline (idx:N или html-id; ведущий # необязателен).
    Содержимое возвращается как есть, без преобразований в markdown/текст.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    path: str = Field(min_length=1, description="Путь к HTML-файлу в workspace.")
    anchor: str = Field(
        min_length=1,
        description=(
            "Anchor заголовка из html_outline (idx:N или html id). "
            "Ведущий '#' необязателен."
        ),
    )
    include_subsections: bool = Field(
        default=True,
        description=(
            "true — включать вложенные подзаголовки (стоп на следующем "
            "заголовке того же или меньшего уровня); false — стоп на любом "
            "следующем заголовке."
        ),
    )
    max_chars: int = Field(
        default=8000,
        ge=100,
        description="Лимит длины ответа в символах.",
    )


@dataclass(frozen=True)
class HtmlSectionToolConfig:
    """DTO tool'а: только prompt overlay."""

    prompt: PromptOverlay


class HtmlSectionTool(HtmlToolBase[SectionArgs, HtmlSectionToolConfig]):
    """HTML фрагмент раздела от заголовка до следующего заголовка."""

    def execute(self, ctx: ToolContext, req: SectionArgs) -> ToolResult:
        try:
            soup = load_soup(self._shell, req.path)
        except WorkspaceNotFoundError as e:
            raise ToolExecutionError(
                tool_id=self.tool_id(), message=f"Файл не найден: {req.path}"
            ) from e
        except WorkspaceError as e:
            raise ToolExecutionError(
                tool_id=self.tool_id(), message=f"Ошибка чтения: {e}"
            ) from e

        headings = collect_headings(soup)
        target = resolve_anchor(headings, req.anchor)
        if target is None:
            raise ToolExecutionError(
                tool_id=self.tool_id(),
                message=(
                    f"Заголовок с anchor={req.anchor!r} не найден; "
                    "получи актуальные anchor'ы через html_outline."
                ),
            )

        target_idx = headings.index(target)
        stop = self._find_stop_heading(headings, target_idx, req.include_subsections)
        html = self._collect_section_html(target.tag, stop.tag if stop else None)

        total_chars = len(html)
        truncated = total_chars > req.max_chars
        if truncated:
            html = html[: req.max_chars]

        return JsonResult(payload={
            "path": req.path,
            "anchor": req.anchor,
            "level": target.level,
            "text": target.text,
            "html": html,
            "chars": len(html),
            "total_chars": total_chars,
            "truncated": truncated,
            "max_chars": req.max_chars,
        })

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
