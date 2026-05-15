"""Tool: копирование файла или директории (cp / cp -r)."""

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

__all__ = ["CpArgs", "CpTool", "CpToolConfig"]


class CpArgs(BaseModel):
    """Скопировать файл или директорию. Для директорий требуется recursive=true."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    src: str = Field(min_length=1, description="Путь источника.")
    dst: str = Field(min_length=1, description="Путь назначения.")
    recursive: bool = Field(
        default=False,
        description="Рекурсивное копирование директории. По умолчанию false.",
    )


@dataclass(frozen=True)
class CpToolConfig:
    prompt: PromptOverlay


class CpTool(FsToolBase[CpArgs, CpToolConfig]):
    """Скопировать файл или директорию."""

    def execute(self, ctx: ToolContext, req: CpArgs) -> ToolResult:
        try:
            self._shell.copy(req.src, req.dst, recursive=req.recursive)
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
