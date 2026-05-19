"""Tool: оглавление HTML-документа из workspace."""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import Field

from boba.tool.html._parse import anchor_for, collect_headings, load_soup
from boba.tool.html.enable import html_enable_if
from boba.tools import FromDI, Scope, tool
from boba.workspace.contract import (
    ProjectWorkspaceShell,
    WorkspaceError,
    WorkspaceNotFoundError,
)

__all__ = ["HtmlOutlineTool"]


@tool(enable_if=html_enable_if("html_outline"))
class HtmlOutlineTool:
    """Иерархия <h1>..<h6> HTML-документа с anchor'ами для html_section.

    Anchor — либо #<id> атрибута заголовка, либо #idx:N (порядковый номер).
    Используется как вход в html_section.
    """

    def __call__(
        self,
        path: Annotated[
            str, Field(min_length=1, description="Путь к HTML-файлу в workspace."),
        ],
        shell: Annotated[ProjectWorkspaceShell, FromDI(Scope.APP)],
        max_depth: Annotated[
            int | None,
            Field(
                ge=1,
                le=6,
                description=(
                    "Максимальный уровень заголовков (1=h1..6=h6). "
                    "Без значения — все 6."
                ),
            ),
        ] = None,
        limit: Annotated[
            int, Field(ge=1, description="Максимум заголовков в ответе."),
        ] = 200,
    ) -> dict[str, Any]:
        try:
            soup = load_soup(shell, path)
        except WorkspaceNotFoundError as e:
            raise RuntimeError(f"Файл не найден: {path}") from e
        except WorkspaceError as e:
            raise RuntimeError(f"Ошибка чтения: {e}") from e

        all_headings = collect_headings(soup, max_depth=max_depth)
        total = len(all_headings)
        truncated = total > limit
        headings = all_headings[:limit] if truncated else all_headings

        title = soup.title.get_text(strip=True) if soup.title else ""
        charset = soup.original_encoding or ""

        return {
            "path": path,
            "title": title,
            "charset": charset,
            "headings": [
                {
                    "index": h.index,
                    "level": h.level,
                    "text": h.text,
                    "anchor": anchor_for(h),
                }
                for h in headings
            ],
            "count": len(headings),
            "total": total,
            "truncated": truncated,
            "limit": limit,
        }
