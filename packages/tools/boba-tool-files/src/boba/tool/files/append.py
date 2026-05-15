"""Tool: дозаписать в конец файла."""

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

__all__ = ["AppendArgs", "AppendTool", "AppendToolConfig"]


class AppendArgs(BaseModel):
    """Дописать текст в конец файла. Если файла нет — создать."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    path: str = Field(min_length=1, description="Путь к файлу.")
    content: str = Field(description="Дописываемый текст.")
    encoding: str = Field(
        default="utf-8",
        min_length=1,
        description="Кодировка файла. По умолчанию 'utf-8'.",
    )


@dataclass(frozen=True)
class AppendToolConfig:
    prompt: PromptOverlay


class AppendTool(FsToolBase[AppendArgs, AppendToolConfig]):
    """Дозаписать текст в конец файла."""

    def execute(self, ctx: ToolContext, req: AppendArgs) -> ToolResult:
        shell = self._shell
        existed = shell.exists(req.path)
        try:
            with shell.append_text(req.path, req.encoding) as f:
                f.write(req.content)
        except WorkspaceError as e:
            raise ToolExecutionError(
                tool_id=self.tool_id(),
                message=f"Ошибка записи: {e}",
            ) from e
        action = "дозаписан" if existed else "создан"
        return TextResult(
            text=f"Файл {action}: {req.path} ({len(req.content)} символов)",
        )
