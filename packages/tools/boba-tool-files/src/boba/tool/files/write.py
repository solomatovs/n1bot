"""Tool: перезаписать файл целиком."""

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

__all__ = ["WriteArgs", "WriteTool", "WriteToolConfig"]


class WriteArgs(BaseModel):
    """Перезаписать файл указанным содержимым.

    Если файла или промежуточных директорий нет — создать.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    path: str = Field(min_length=1, description="Путь к файлу.")
    content: str = Field(description="Новое содержимое файла.")
    encoding: str = Field(
        default="utf-8",
        min_length=1,
        description="Кодировка файла. По умолчанию 'utf-8'.",
    )


@dataclass(frozen=True)
class WriteToolConfig:
    prompt: PromptOverlay


class WriteTool(FsToolBase[WriteArgs, WriteToolConfig]):
    """Полностью перезаписать файл содержимым."""

    def execute(self, ctx: ToolContext, req: WriteArgs) -> ToolResult:
        shell = self._shell
        existed = shell.exists(req.path)
        try:
            with shell.write_text(req.path, req.encoding) as f:
                f.write(req.content)
        except WorkspaceError as e:
            raise ToolExecutionError(
                tool_id=self.tool_id(),
                message=f"Ошибка записи: {e}",
            ) from e
        action = "обновлён" if existed else "создан"
        return TextResult(
            text=f"Файл {action}: {req.path} ({len(req.content)} символов)",
        )
