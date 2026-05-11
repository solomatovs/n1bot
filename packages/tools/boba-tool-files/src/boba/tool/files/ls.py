"""Tool: список элементов workspace без рекурсии."""

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

__all__ = ["LsArgs", "LsTool", "LsToolConfig"]


@dataclass(frozen=True)
class LsArgs:
    """Перечислить содержимое директории на одном уровне без рекурсии.

    При переполнении limit ответ обрезается с маркером '(truncated at limit=N)'.
    Для рекурсии — tree.
    """

    path: Annotated[
        str | None, "Путь директории. Без значения — корень workspace.", NonEmpty()
    ] = None
    limit: Annotated[
        int, "Максимум элементов в ответе. По умолчанию 200.", MinValue(1)
    ] = 200


@dataclass(frozen=True)
class LsToolConfig:
    prompt: PromptOverlay


class LsTool(FsToolBase[LsArgs, LsToolConfig]):
    """Плоский список элементов workspace (без рекурсии)."""

    def execute(self, ctx: ToolContext, req: LsArgs) -> ToolResult:
        try:
            iterator = self._shell.ls(req.path)
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
