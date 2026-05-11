"""Tool: перезаписать файл целиком."""

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
from boba.workspace.contract import WorkspaceError

__all__ = ["WriteArgs", "WriteTool", "WriteToolConfig"]


@dataclass(frozen=True)
class WriteArgs:
    """Перезаписать файл указанным содержимым.

    Если файла или промежуточных директорий нет — создать.
    """

    path: Annotated[str, "Путь к файлу.", NonEmpty()]
    content: Annotated[str, "Новое содержимое файла."]
    encoding: Annotated[
        str, "Кодировка файла. По умолчанию 'utf-8'.", NonEmpty()
    ] = "utf-8"


@dataclass(frozen=True)
class WriteToolConfig:
    prompt: PromptOverlay


class WriteTool(FsToolBase[WriteArgs, WriteToolConfig]):
    """Полностью перезаписать файл содержимым."""

    def execute(self, ctx: ToolContext, req: WriteArgs) -> ToolResult:
        shell = self._shell(ctx)
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
