"""Tool: метаданные файла или директории."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated

from boba.plugin.prompt import PromptOverlay
from boba.schema.coercion import NonEmpty
from boba.tool.files._base import FsToolBase
from boba.tools.domain import (
    JsonResult,
    ToolContext,
    ToolExecutionError,
    ToolResult,
)
from boba.workspace.contract import WorkspaceError, WorkspaceNotFoundError

__all__ = ["StatArgs", "StatTool", "StatToolConfig"]


@dataclass(frozen=True)
class StatArgs:
    """Вернуть метаданные ресурса.

    Тип (file/directory/other), размер в байтах, время модификации. Если
    ресурса нет — ошибка. Для директорий size — размер inode-блока ФС, не
    количество файлов; для содержимого директории — ls/tree.
    """

    path: Annotated[str, "Путь к файлу или директории.", NonEmpty()]


@dataclass(frozen=True)
class StatToolConfig:
    prompt: PromptOverlay


class StatTool(FsToolBase[StatArgs, StatToolConfig]):
    """Метаданные файла или директории."""

    def execute(self, ctx: ToolContext, req: StatArgs) -> ToolResult:
        try:
            meta = self._shell(ctx).meta(req.path)
        except WorkspaceNotFoundError as e:
            raise ToolExecutionError(
                tool_id=self.tool_id(),
                message=f"Не найдено: {req.path}",
            ) from e
        except WorkspaceError as e:
            raise ToolExecutionError(
                tool_id=self.tool_id(),
                message=f"Ошибка stat: {e}",
            ) from e

        body = {
            "path": meta.path,
            "kind": meta.kind,
            "size": meta.size,
            "modified": meta.modified.isoformat(),
        }

        return JsonResult(body)
