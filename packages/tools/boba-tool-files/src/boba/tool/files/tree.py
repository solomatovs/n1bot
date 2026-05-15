"""Tool: рекурсивный обход workspace."""

from __future__ import annotations

from dataclasses import dataclass
from itertools import islice

from pydantic import BaseModel, ConfigDict, Field

from boba.plugin.prompt import PromptOverlay
from boba.tool.files._base import FsToolBase
from boba.tools.domain import (
    JsonResult,
    ToolContext,
    ToolExecutionError,
    ToolResult,
)
from boba.workspace.contract import WorkspaceError

__all__ = ["TreeArgs", "TreeTool", "TreeToolConfig"]


class TreeArgs(BaseModel):
    """Рекурсивно перечислить все файлы под директорией.

    Плоский список путей. При переполнении limit ответ обрезается с маркером
    '(truncated at limit=N)'. Для одного уровня — ls.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    limit: int = Field(ge=1, description="Максимум путей в ответе.")
    path: str | None = Field(
        default=None,
        min_length=1,
        description="Корень обхода. Без значения — корень workspace.",
    )


@dataclass(frozen=True)
class TreeToolConfig:
    prompt: PromptOverlay


class TreeTool(FsToolBase[TreeArgs, TreeToolConfig]):
    """Рекурсивный обход всех файлов workspace."""

    def execute(self, ctx: ToolContext, req: TreeArgs) -> ToolResult:
        try:
            iterator = self._shell.tree(req.path)
            items = list(islice(iterator, req.limit + 1))
        except WorkspaceError as e:
            raise ToolExecutionError(
                tool_id=self.tool_id(), message=f"Ошибка обхода: {e}",
            ) from e

        total = len(items)
        truncated = total > req.limit
        if truncated:
            items = items[: req.limit]

        return JsonResult(payload={
            "location": req.path or "/",
            "items": items,
            "count": len(items),
            "truncated": truncated,
            "limit": req.limit,
        })
