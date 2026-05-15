"""Tool: удаление файла или директории (rm / rm -r)."""

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

__all__ = ["RmArgs", "RmTool", "RmToolConfig"]


class RmArgs(BaseModel):
    """Удалить файл или директорию.

    Для директорий требуется recursive=true. Безвозвратно.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    path: str = Field(min_length=1, description="Путь к файлу или директории.")
    recursive: bool = Field(
        default=False,
        description="Удалить директорию со всем содержимым. По умолчанию false.",
    )


@dataclass(frozen=True)
class RmToolConfig:
    prompt: PromptOverlay


class RmTool(FsToolBase[RmArgs, RmToolConfig]):
    """Удалить файл или директорию."""

    def execute(self, ctx: ToolContext, req: RmArgs) -> ToolResult:
        try:
            self._shell.delete(req.path, recursive=req.recursive)
        except WorkspaceNotFoundError as e:
            raise ToolExecutionError(
                tool_id=self.tool_id(),
                message=f"Не найдено: {req.path}",
            ) from e
        except WorkspaceError as e:
            raise ToolExecutionError(
                tool_id=self.tool_id(),
                message=f"Ошибка удаления: {e}",
            ) from e
        return TextResult(text=f"Удалено: {req.path}")
