"""Tool: рекурсивный обход workspace."""

from __future__ import annotations

from dataclasses import dataclass
from itertools import islice
from typing import Annotated

from boba.plugin.prompt import PromptOverlay
from boba.schema.coercion import MinValue, NonEmpty
from boba.tool.files._base import FsToolBase
from boba.tools.domain import (
    JsonResult,
    ToolContext,
    ToolExecutionError,
    ToolResult,
)
from boba.workspace.contract import WorkspaceError

__all__ = ["TreeArgs", "TreeTool", "TreeToolConfig"]


@dataclass(frozen=True)
class TreeArgs:
    """Рекурсивно перечислить все файлы под директорией.

    Плоский список путей. При переполнении limit ответ обрезается с маркером
    '(truncated at limit=N)'. Для одного уровня — ls.
    """

    limit: Annotated[int, "Максимум путей в ответе.", MinValue(1)]
    path: Annotated[
        str | None, "Корень обхода. Без значения — корень workspace.", NonEmpty()
    ] = None


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
