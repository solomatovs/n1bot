"""Tool: создать пустой файл или обновить mtime."""

from __future__ import annotations

from typing import Annotated

from pydantic import Field

from boba.tools import FromDI, Scope, tool
from boba.workspace.contract import ProjectWorkspaceShell, WorkspaceError

__all__ = ["touch"]


@tool
def touch(
    path: Annotated[str, Field(min_length=1, description="Путь к файлу.")],
    shell: Annotated[ProjectWorkspaceShell, FromDI(Scope.APP)],
) -> str:
    """Создать пустой файл (включая промежуточные директории).

    Если уже существует — обновить время модификации, содержимое не трогать.
    """
    try:
        shell.touch(path)
    except WorkspaceError as e:
        raise RuntimeError(f"Ошибка touch: {e}") from e
    return f"touch: {path}"
