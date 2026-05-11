"""Tool: создать пустой файл или обновить mtime."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated

from boba.plugin.prompt import PromptOverlay
from boba.schema.coercion import NonEmpty
from boba.tools.domain import (
    TextResult,
    Tool,
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


class TouchTool(Tool[TouchArgs, TouchToolConfig]):
    """Создать пустой файл или обновить mtime существующего."""

    def execute(self, ctx: ToolContext, req: TouchArgs) -> ToolResult:
        try:
            ctx.project_workspace.touch(req.path)
        except WorkspaceError as e:
            raise ToolExecutionError(
                tool_id=self.tool_id(),
                message=f"Ошибка touch: {e}",
            ) from e
        return TextResult(text=f"touch: {req.path}")
