"""Tool: список элементов workspace без рекурсии."""

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

__all__ = ["LsArgs", "LsTool", "LsToolConfig"]


class LsArgs(BaseModel):
    """Перечислить содержимое директории на одном уровне без рекурсии.

    При переполнении limit ответ обрезается с маркером '(truncated at limit=N)'.
    Для рекурсии — tree.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    path: str | None = Field(
        default=None,
        min_length=1,
        description="Путь директории. Без значения — корень workspace.",
    )
    limit: int = Field(
        default=200,
        ge=1,
        description="Максимум элементов в ответе. По умолчанию 200.",
    )


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
