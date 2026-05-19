"""Tool: find-and-replace редактирование файла."""

from __future__ import annotations

from typing import Annotated

from pydantic import Field

from boba.tool.files.enable import files_enable_if
from boba.tools import FromDI, Scope, tool
from boba.workspace.contract import (
    ProjectWorkspaceShell,
    WorkspaceError,
    WorkspaceNotFoundError,
)

__all__ = ["EditTool"]


@tool(enable_if=files_enable_if("edit"))
class EditTool:
    """Find-and-replace редактирование текстового файла.

    По умолчанию old_string должна встречаться в файле ровно один раз — иначе
    ошибка. С replace_all=true заменяются все вхождения. Совпадение точное,
    посимвольное.
    """

    def __call__(
        self,
        path: Annotated[str, Field(min_length=1, description="Путь к файлу.")],
        old_string: Annotated[
            str,
            Field(min_length=1, description="Подстрока для замены. Совпадение точное."),
        ],
        new_string: Annotated[
            str, Field(description="Заменяющий текст. Пустая строка = удаление."),
        ],
        shell: Annotated[ProjectWorkspaceShell, FromDI(Scope.APP)],
        replace_all: Annotated[
            bool, Field(description="Заменить все вхождения. По умолчанию false."),
        ] = False,
        encoding: Annotated[
            str,
            Field(min_length=1, description="Кодировка файла. По умолчанию 'utf-8'."),
        ] = "utf-8",
    ) -> str:
        try:
            applied = shell.edit_text(
                path,
                old_string,
                new_string,
                replace_all=replace_all,
                encoding=encoding,
            )
        except WorkspaceNotFoundError as e:
            raise RuntimeError(f"Файл не найден: {path}") from e
        except WorkspaceError as e:
            raise RuntimeError(f"Ошибка edit: {e}") from e
        return f"Заменено в {path}: {applied} вхождение(й)."
