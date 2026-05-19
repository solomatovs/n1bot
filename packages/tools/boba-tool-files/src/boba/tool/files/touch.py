"""Tool: создать пустой файл или обновить mtime."""

from __future__ import annotations

from typing import Annotated

from pydantic import Field

from boba.tool.files.enable import files_enable_if
from boba.tools import FromDI, Scope, tool
from boba.workspace.contract import ProjectWorkspaceShell, WorkspaceError

__all__ = ["TouchTool"]


@tool(enable_if=files_enable_if("touch"))
class TouchTool:
    """Создать пустой файл (включая промежуточные директории).

    Если уже существует — обновить время модификации, содержимое не трогать.
    """

    def __call__(
        self,
        path: Annotated[str, Field(min_length=1, description="Путь к файлу.")],
        shell: Annotated[ProjectWorkspaceShell, FromDI(Scope.APP)],
    ) -> str:
        try:
            shell.touch(path)
        except WorkspaceError as e:
            raise RuntimeError(f"Ошибка touch: {e}") from e
        return f"touch: {path}"
