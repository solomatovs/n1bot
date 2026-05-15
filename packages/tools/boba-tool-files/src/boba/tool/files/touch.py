"""Tool: создать пустой файл или обновить mtime."""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict, Field

from boba.plugin.prompt import PromptOverlay
from boba.tool.files._base import FsToolBase
from boba.tools.domain import (
    TextResult,
    ToolContext,
    ToolExecutionError,
    ToolResult,
)
from boba.workspace.contract import WorkspaceError

__all__ = ["TouchArgs", "TouchTool", "TouchToolConfig"]


class TouchArgs(BaseModel):
    """Создать пустой файл (включая промежуточные директории).

    Если уже существует — обновить время модификации, содержимое не трогать.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    path: str = Field(min_length=1, description="Путь к файлу.")


@dataclass(frozen=True)
class TouchToolConfig:
    prompt: PromptOverlay


class TouchTool(FsToolBase[TouchArgs, TouchToolConfig]):
    """Создать пустой файл или обновить mtime существующего."""

    def execute(self, ctx: ToolContext, req: TouchArgs) -> ToolResult:
        try:
            self._shell.touch(req.path)
        except WorkspaceError as e:
            raise ToolExecutionError(
                tool_id=self.tool_id(),
                message=f"Ошибка touch: {e}",
            ) from e
        return TextResult(text=f"touch: {req.path}")
