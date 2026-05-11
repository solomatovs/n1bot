"""Tool: создать директорию."""

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

__all__ = ["MkdirArgs", "MkdirTool", "MkdirToolConfig"]


@dataclass(frozen=True)
class MkdirArgs:
    """Создать директорию (включая промежуточные).

    Если уже существует — no-op. Если по пути файл — ошибка.
    """

    path: Annotated[str, "Путь создаваемой директории.", NonEmpty()]


@dataclass(frozen=True)
class MkdirToolConfig:
    prompt: PromptOverlay


class MkdirTool(Tool[MkdirArgs, MkdirToolConfig]):
    """Создать директорию."""

    def execute(self, ctx: ToolContext, req: MkdirArgs) -> ToolResult:
        try:
            ctx.project_workspace.mkdir(req.path)
        except WorkspaceError as e:
            raise ToolExecutionError(
                tool_id=self.tool_id(),
                message=f"Ошибка mkdir: {e}",
            ) from e
        return TextResult(text=f"Директория создана: {req.path}")
