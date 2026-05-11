"""Tool: оглавление HTML-документа из workspace."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated

from boba.plugin.prompt import PromptOverlay
from boba.schema.coercion import MaxValue, MinValue, NonEmpty
from boba.tool.html._parse import Heading, anchor_for, collect_headings, load_soup
from boba.tools.domain import (
    TextResult,
    Tool,
    ToolContext,
    ToolExecutionError,
    ToolResult,
)
from boba.workspace.contract import (
    WorkspaceError,
    WorkspaceNotFoundError,
)

__all__ = ["HtmlOutlineTool", "HtmlOutlineToolConfig", "OutlineArgs"]


@dataclass(frozen=True)
class OutlineArgs:
    """Оглавление HTML-файла: иерархия <h1>..<h6> с anchor'ами.

    Anchor — либо #<id> атрибута заголовка, либо #idx:N (порядковый номер).
    Используется как вход в html_section.
    """

    path: Annotated[str, "Путь к HTML-файлу в workspace.", NonEmpty()]
    max_depth: Annotated[
        int | None,
        "Максимальный уровень заголовков (1=h1..6=h6). Без значения — все 6.",
        MinValue(1),
        MaxValue(6),
    ] = None
    limit: Annotated[int, "Максимум заголовков в ответе.", MinValue(1)] = 200


@dataclass(frozen=True)
class HtmlOutlineToolConfig:
    """DTO tool'а: только prompt overlay (workspace передаётся через ToolContext)."""

    prompt: PromptOverlay


class HtmlOutlineTool(Tool[OutlineArgs, HtmlOutlineToolConfig]):
    """Иерархия <h1>..<h6> HTML-документа с anchor'ами для html_section."""

    def execute(self, ctx: ToolContext, req: OutlineArgs) -> ToolResult:
        try:
            soup = load_soup(ctx.project_workspace, req.path)
        except WorkspaceNotFoundError as e:
            raise ToolExecutionError(
                tool_id=self.tool_id(), message=f"Файл не найден: {req.path}"
            ) from e
        except WorkspaceError as e:
            raise ToolExecutionError(
                tool_id=self.tool_id(), message=f"Ошибка чтения: {e}"
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

        body = "\n".join(self._render_line(h) for h in headings)
        suffix = f", truncated at limit={req.limit}" if truncated else ""
        return TextResult(
            text=f"{head}\nЗаголовков: {len(headings)}{suffix}\n\n{body}",
        )

    @staticmethod
    def _render_line(h: Heading) -> str:
        indent = "  " * (h.level - 1)
        return f"{h.index:>3}. {indent}h{h.level} {h.text}  #{anchor_for(h)}"
