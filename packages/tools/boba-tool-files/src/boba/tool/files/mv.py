"""Tool: переместить/переименовать файл или директорию."""

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

__all__ = ["MvArgs", "MvTool", "MvToolConfig"]


@dataclass(frozen=True)
class MvArgs:
    """Переместить или переименовать файл/директорию.

    Если dst — существующая директория, src переносится внутрь. Файл по пути
    dst перезаписывается. Промежуточные директории не создаются.
    """

    src: Annotated[str, "Путь источника.", NonEmpty()]
    dst: Annotated[str, "Путь назначения.", NonEmpty()]


@dataclass(frozen=True)
class MvToolConfig:
    prompt: PromptOverlay


class MvTool(FsToolBase[MvArgs, MvToolConfig]):
    """Переместить/переименовать файл или директорию."""

    def execute(self, ctx: ToolContext, req: MvArgs) -> ToolResult:
        try:
            self._shell.move(req.src, req.dst)
        except WorkspaceNotFoundError as e:
            raise ToolExecutionError(
                tool_id=self.tool_id(),
                message=f"Источник не найден: {req.src}",
            ) from e
        except WorkspaceError as e:
            raise ToolExecutionError(
                tool_id=self.tool_id(),
                message=f"Ошибка перемещения: {e}",
            ) from e
        return TextResult(text=f"Перемещено: {req.src} → {req.dst}")
