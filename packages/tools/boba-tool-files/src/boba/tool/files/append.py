"""Tool: дозаписать в конец файла."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated

from boba.plugin.prompt import PromptOverlay
from boba.schema.coercion import NonEmpty
from boba.tools.domain import (
    TextResult,
    Tool,
    ToolContext,
    ToolExecutionError,
    ToolResult,
)
from boba.workspace.contract import WorkspaceError

__all__ = ["AppendArgs", "AppendTool", "AppendToolConfig"]


@dataclass(frozen=True)
class AppendArgs:
    """Дописать текст в конец файла. Если файла нет — создать."""

    path: Annotated[str, "Путь к файлу.", NonEmpty()]
    content: Annotated[str, "Дописываемый текст."]
    encoding: Annotated[
        str, "Кодировка файла. По умолчанию 'utf-8'.", NonEmpty()
    ] = "utf-8"


@dataclass(frozen=True)
class AppendToolConfig:
    prompt: PromptOverlay


class AppendTool(Tool[AppendArgs, AppendToolConfig]):
    """Дозаписать текст в конец файла."""

    def execute(self, ctx: ToolContext, req: AppendArgs) -> ToolResult:
        existed = ctx.project_workspace.exists(req.path)
        try:
            with ctx.project_workspace.append_text(req.path, req.encoding) as f:
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
