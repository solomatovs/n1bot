"""Tool: удаление файла или директории (rm / rm -r)."""

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

__all__ = ["RmTool"]


@tool(enable_if=files_enable_if("rm"))
class RmTool:
    """Удалить файл или директорию.

    Для директорий требуется recursive=true. Безвозвратно.
    """

    def __call__(
        self,
        path: Annotated[
            str, Field(min_length=1, description="Путь к файлу или директории."),
        ],
        shell: Annotated[ProjectWorkspaceShell, FromDI(Scope.APP)],
        recursive: Annotated[
            bool,
            Field(
                description="Удалить директорию со всем содержимым. По умолчанию false.",
            ),
        ] = False,
    ) -> str:
        try:
            shell.delete(path, recursive=recursive)
        except WorkspaceNotFoundError as e:
            raise RuntimeError(f"Не найдено: {path}") from e
        except WorkspaceError as e:
            raise RuntimeError(f"Ошибка удаления: {e}") from e
        return f"Удалено: {path}"
