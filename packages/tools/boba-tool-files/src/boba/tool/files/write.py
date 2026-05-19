"""Tool: перезаписать файл целиком."""

from __future__ import annotations

from typing import Annotated

from pydantic import Field

from boba.tools import FromDI, Scope, tool
from boba.workspace.contract import ProjectWorkspaceShell, WorkspaceError

__all__ = ["write"]


@tool
def write(
    path: Annotated[str, Field(min_length=1, description="Путь к файлу.")],
    content: Annotated[str, Field(description="Новое содержимое файла.")],
    shell: Annotated[ProjectWorkspaceShell, FromDI(Scope.APP)],
    encoding: Annotated[
        str,
        Field(min_length=1, description="Кодировка файла. По умолчанию 'utf-8'."),
    ] = "utf-8",
) -> str:
    """Полностью перезаписать файл содержимым.

    Если файла или промежуточных директорий нет — создать.
    """
    existed = shell.exists(path)
    try:
        with shell.write_text(path, encoding) as f:
            f.write(content)
    except WorkspaceError as e:
        raise RuntimeError(f"Ошибка записи: {e}") from e
    action = "обновлён" if existed else "создан"
    return f"Файл {action}: {path} ({len(content)} символов)"
