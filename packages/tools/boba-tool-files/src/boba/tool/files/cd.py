"""Tool: сменить текущую директорию."""

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
from boba.workspace.contract import WorkspaceError, WorkspaceNotFoundError

__all__ = ["CdArgs", "CdTool", "CdToolConfig"]


class CdArgs(BaseModel):
    """Сменить текущую директорию."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    path: str = Field(min_length=1, description="Путь директории.")


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
