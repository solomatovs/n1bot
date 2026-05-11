"""Tool: find-and-replace редактирование файла."""

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

__all__ = ["EditArgs", "EditTool", "EditToolConfig"]


@dataclass(frozen=True)
class EditArgs:
    """Заменить подстроку old_string на new_string.

    По умолчанию old_string должна встречаться в файле ровно один раз — иначе
    ошибка. С replace_all=true заменяются все вхождения. Совпадение точное,
    посимвольное.
    """

    path: Annotated[str, "Путь к файлу.", NonEmpty()]
    old_string: Annotated[str, "Подстрока для замены. Совпадение точное.", NonEmpty()]
    new_string: Annotated[str, "Заменяющий текст. Пустая строка = удаление."]
    replace_all: Annotated[bool, "Заменить все вхождения. По умолчанию false."] = False
    encoding: Annotated[
        str, "Кодировка файла. По умолчанию 'utf-8'.", NonEmpty()
    ] = "utf-8"


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
