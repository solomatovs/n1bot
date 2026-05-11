"""Tool: создать пустой файл или обновить mtime."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated

from boba.plugin.prompt import PromptOverlay
from boba.schema.coercion import NonEmpty
from boba.tool.files._base import FsToolBase
from boba.tools.domain import (
    TextResult,
    ToolContext,
    ToolExecutionError,
    ToolResult,
)
from boba.workspace.contract import WorkspaceError

__all__ = ["TouchArgs", "TouchTool", "TouchToolConfig"]


@dataclass(frozen=True)
class TouchArgs:
    """Создать пустой файл (включая промежуточные директории).

    Если уже существует — обновить время модификации, содержимое не трогать.
    """

    path: Annotated[str, "Путь к файлу.", NonEmpty()]


@dataclass(frozen=True)
class TouchToolConfig:
    prompt: PromptOverlay


class TouchTool(FsToolBase[TouchArgs, TouchToolConfig]):
    """Создать пустой файл или обновить mtime существующего."""

    def execute(self, ctx: ToolContext, req: TouchArgs) -> ToolResult:
        try:
            self._shell(ctx).touch(req.path)
        except WorkspaceError as e:
            raise ToolExecutionError(
                tool_id=self.tool_id(),
                message=f"Ошибка touch: {e}",
            ) from e
        return TextResult(text=f"touch: {req.path}")
