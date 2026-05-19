"""Tool: содержимое раздела HTML по anchor."""

from __future__ import annotations

from typing import Annotated, Any

from bs4.element import Tag
from pydantic import Field

from boba.tool.html._parse import (
    Heading,
    collect_headings,
    load_soup,
    resolve_anchor,
)
from boba.tool.html.enable import html_enable_if
from boba.tools import FromDI, Scope, tool
from boba.workspace.contract import (
    ProjectWorkspaceShell,
    WorkspaceError,
    WorkspaceNotFoundError,
)

__all__ = ["HtmlSectionTool"]


@tool(enable_if=html_enable_if("html_section"))
class HtmlSectionTool:
    """HTML фрагмент раздела от выбранного заголовка до следующего.

    anchor берётся из html_outline (idx:N или html-id; ведущий '#'
    необязателен). Содержимое возвращается как есть, без преобразований
    в markdown/текст.
    """

    def __call__(
        self,
        path: Annotated[
            str, Field(min_length=1, description="Путь к HTML-файлу в workspace."),
        ],
        anchor: Annotated[
            str,
            Field(
                min_length=1,
                description=(
                    "Anchor заголовка из html_outline (idx:N или html id). "
                    "Ведущий '#' необязателен."
                ),
            ),
        ],
        shell: Annotated[ProjectWorkspaceShell, FromDI(Scope.APP)],
        include_subsections: Annotated[
            bool,
            Field(
                description=(
                    "true — включать вложенные подзаголовки (стоп на следующем "
                    "заголовке того же или меньшего уровня); false — стоп на "
                    "любом следующем заголовке."
                ),
            ),
        ] = True,
        max_chars: Annotated[
            int, Field(ge=100, description="Лимит длины ответа в символах."),
        ] = 8000,
    ) -> dict[str, Any]:
        try:
            soup = load_soup(shell, path)
        except WorkspaceNotFoundError as e:
            raise RuntimeError(f"Файл не найден: {path}") from e
        except WorkspaceError as e:
            raise RuntimeError(f"Ошибка чтения: {e}") from e

        headings = collect_headings(soup)
        target = resolve_anchor(headings, anchor)
        if target is None:
            raise RuntimeError(
                f"Заголовок с anchor={anchor!r} не найден; "
                "получи актуальные anchor'ы через html_outline.",
            )

        target_idx = headings.index(target)
        stop = _find_stop_heading(headings, target_idx, include_subsections)
        html = _collect_section_html(target.tag, stop.tag if stop else None)

        total_chars = len(html)
        truncated = total_chars > max_chars
        if truncated:
            html = html[:max_chars]

        return {
            "path": path,
            "anchor": anchor,
            "level": target.level,
            "text": target.text,
            "html": html,
            "chars": len(html),
            "total_chars": total_chars,
            "truncated": truncated,
            "max_chars": max_chars,
        }


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
