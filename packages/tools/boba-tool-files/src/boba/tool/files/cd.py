"""Tool: сменить текущую директорию."""

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
from boba.workspace.contract import WorkspaceError, WorkspaceNotFoundError

__all__ = ["CdArgs", "CdTool", "CdToolConfig"]


@dataclass(frozen=True)
class CdArgs:
    """Сменить текущую директорию."""

    path: Annotated[str, "Путь директории.", NonEmpty()]


@dataclass(frozen=True)
class CdToolConfig:
    prompt: PromptOverlay


class CdTool(FsToolBase[CdArgs, CdToolConfig]):
    """Сменить текущую директорию."""

    def execute(self, ctx: ToolContext, req: CdArgs) -> ToolResult:
        shell = self._shell
        try:
            shell.cd(req.path)
        except WorkspaceNotFoundError as e:
            raise ToolExecutionError(
                tool_id=self.tool_id(),
                message=f"Директория не найдена: {req.path}",
            ) from e
        except WorkspaceError as e:
            raise ToolExecutionError(
                tool_id=self.tool_id(),
                message=f"Ошибка cd: {e}",
            ) from e
        return TextResult(text=f"Текущая директория: {shell.cwd}")
