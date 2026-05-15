"""Tool: оглавление HTML-документа из workspace."""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict, Field

from boba.plugin.prompt import PromptOverlay
from boba.tool.html._base import HtmlToolBase
from boba.tool.html._parse import anchor_for, collect_headings, load_soup
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

__all__ = ["HtmlOutlineTool", "HtmlOutlineToolConfig", "OutlineArgs"]


class OutlineArgs(BaseModel):
    """Оглавление HTML-файла: иерархия <h1>..<h6> с anchor'ами.

    Anchor — либо #<id> атрибута заголовка, либо #idx:N (порядковый номер).
    Используется как вход в html_section.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    path: str = Field(min_length=1, description="Путь к HTML-файлу в workspace.")
    max_depth: int | None = Field(
        default=None,
        ge=1,
        le=6,
        description=(
            "Максимальный уровень заголовков (1=h1..6=h6). Без значения — все 6."
        ),
    )
    limit: int = Field(default=200, ge=1, description="Максимум заголовков в ответе.")


@dataclass(frozen=True)
class HtmlOutlineToolConfig:
    """DTO tool'а: только prompt overlay (workspace передаётся через ToolContext)."""

    prompt: PromptOverlay


class HtmlOutlineTool(HtmlToolBase[OutlineArgs, HtmlOutlineToolConfig]):
    """Иерархия <h1>..<h6> HTML-документа с anchor'ами для html_section."""

    def execute(self, ctx: ToolContext, req: OutlineArgs) -> ToolResult:
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

        all_headings = collect_headings(soup, max_depth=req.max_depth)
        total = len(all_headings)
        truncated = total > req.limit
        headings = all_headings[: req.limit] if truncated else all_headings

        title = soup.title.get_text(strip=True) if soup.title else ""
        charset = soup.original_encoding or ""

        return JsonResult(payload={
            "path": req.path,
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
            "limit": req.limit,
        })
