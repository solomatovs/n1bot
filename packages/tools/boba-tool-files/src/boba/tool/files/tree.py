"""Tool: рекурсивный обход workspace."""

from __future__ import annotations

from dataclasses import dataclass
from itertools import islice
from typing import Annotated

from boba.plugin.prompt import PromptOverlay
from boba.schema.coercion import MinValue, NonEmpty
from boba.tool.files._base import FsToolBase
from boba.tools.domain import (
    TextResult,
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
            iterator = self._shell(ctx).tree(req.path)
            items = list(islice(iterator, req.limit + 1))
        except WorkspaceError as e:
            raise ToolExecutionError(
                tool_id=self.tool_id(), message=f"Ошибка обхода: {e}",
            ) from e

        truncated = len(items) > req.limit
        if truncated:
            items = items[: req.limit]

        location = req.path or "/"

        if not items:
            return TextResult(text=f"{location} пуст.")

        header = f"Файлы {location} ({len(items)}, лимит={req.limit}"
        if truncated:
            header += f", truncated at limit={req.limit}"
        header += "):"
        body = "\n".join(f"- {p}" for p in items)
        return TextResult(text=f"{header}\n{body}")
