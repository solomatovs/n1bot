"""Tool: удаление файла или директории (rm / rm -r)."""

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

__all__ = ["RmArgs", "RmTool", "RmToolConfig"]


@dataclass(frozen=True)
class RmArgs:
    """Удалить файл или директорию.

    Для директорий требуется recursive=true. Безвозвратно.
    """

    path: Annotated[str, "Путь к файлу или директории.", NonEmpty()]
    recursive: Annotated[
        bool, "Удалить директорию со всем содержимым. По умолчанию false."
    ] = False


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
