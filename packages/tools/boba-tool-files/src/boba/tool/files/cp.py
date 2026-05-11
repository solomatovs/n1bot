"""Tool: копирование файла или директории (cp / cp -r)."""

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

__all__ = ["CpArgs", "CpTool", "CpToolConfig"]


@dataclass(frozen=True)
class CpArgs:
    """Скопировать файл или директорию. Для директорий требуется recursive=true."""

    src: Annotated[str, "Путь источника.", NonEmpty()]
    dst: Annotated[str, "Путь назначения.", NonEmpty()]
    recursive: Annotated[
        bool, "Рекурсивное копирование директории. По умолчанию false."
    ] = False


@dataclass(frozen=True)
class CpToolConfig:
    prompt: PromptOverlay


class CpTool(FsToolBase[CpArgs, CpToolConfig]):
    """Скопировать файл или директорию."""

    def execute(self, ctx: ToolContext, req: CpArgs) -> ToolResult:
        try:
            self._shell(ctx).copy(req.src, req.dst, recursive=req.recursive)
        except WorkspaceNotFoundError as e:
            raise ToolExecutionError(
                tool_id=self.tool_id(),
                message=f"Источник не найден: {req.src}",
            ) from e
        except WorkspaceError as e:
            raise ToolExecutionError(
                tool_id=self.tool_id(),
                message=f"Ошибка копирования: {e}",
            ) from e
        return TextResult(text=f"Скопировано: {req.src} → {req.dst}")
