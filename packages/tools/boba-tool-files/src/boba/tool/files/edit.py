"""Tool: find-and-replace редактирование файла."""

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

__all__ = ["EditArgs", "EditTool", "EditToolConfig"]


class EditArgs(BaseModel):
    """Заменить подстроку old_string на new_string.

    По умолчанию old_string должна встречаться в файле ровно один раз — иначе
    ошибка. С replace_all=true заменяются все вхождения. Совпадение точное,
    посимвольное.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    path: str = Field(min_length=1, description="Путь к файлу.")
    old_string: str = Field(
        min_length=1,
        description="Подстрока для замены. Совпадение точное.",
    )
    new_string: str = Field(description="Заменяющий текст. Пустая строка = удаление.")
    replace_all: bool = Field(
        default=False,
        description="Заменить все вхождения. По умолчанию false.",
    )
    encoding: str = Field(
        default="utf-8",
        min_length=1,
        description="Кодировка файла. По умолчанию 'utf-8'.",
    )


@dataclass(frozen=True)
class EditToolConfig:
    prompt: PromptOverlay


class EditTool(FsToolBase[EditArgs, EditToolConfig]):
    """Find-and-replace редактирование текстового файла."""

    def execute(self, ctx: ToolContext, req: EditArgs) -> ToolResult:
        try:
            applied = self._shell.edit_text(
                req.path,
                req.old_string,
                req.new_string,
                replace_all=req.replace_all,
                encoding=req.encoding,
            )
        except WorkspaceNotFoundError as e:
            raise ToolExecutionError(
                tool_id=self.tool_id(),
                message=f"Файл не найден: {req.path}",
            ) from e
        except WorkspaceError as e:
            raise ToolExecutionError(
                tool_id=self.tool_id(),
                message=f"Ошибка edit: {e}",
            ) from e
        return TextResult(
            text=f"Заменено в {req.path}: {applied} вхождение(й).",
        )
