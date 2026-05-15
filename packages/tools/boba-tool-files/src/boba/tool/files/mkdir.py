"""Tool: создать директорию."""

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

__all__ = ["MkdirArgs", "MkdirTool", "MkdirToolConfig"]


class MkdirArgs(BaseModel):
    """Создать директорию (включая промежуточные).

    Если уже существует — no-op. Если по пути файл — ошибка.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    path: str = Field(min_length=1, description="Путь создаваемой директории.")


@dataclass(frozen=True)
class MkdirToolConfig:
    prompt: PromptOverlay


class MkdirTool(FsToolBase[MkdirArgs, MkdirToolConfig]):
    """Создать директорию."""

    def execute(self, ctx: ToolContext, req: MkdirArgs) -> ToolResult:
        try:
            self._shell.mkdir(req.path)
        except WorkspaceError as e:
            raise ToolExecutionError(
                tool_id=self.tool_id(),
                message=f"Ошибка mkdir: {e}",
            ) from e
        return TextResult(text=f"Директория создана: {req.path}")
