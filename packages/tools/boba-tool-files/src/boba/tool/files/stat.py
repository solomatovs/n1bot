"""Tool: метаданные файла или директории."""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import Field

from boba.tool.files.enable import files_enable_if
from boba.tools import FromDI, Scope, tool
from boba.workspace.contract import (
    ProjectWorkspaceShell,
    WorkspaceError,
    WorkspaceNotFoundError,
)

__all__ = ["stat"]


@tool(enable_if=files_enable_if("stat"))
def stat(
    path: Annotated[
        str, Field(min_length=1, description="Путь к файлу или директории."),
    ],
    shell: Annotated[ProjectWorkspaceShell, FromDI(Scope.APP)],
) -> dict[str, Any]:
    """Метаданные файла или директории.

    Тип (file/directory/other), размер в байтах, время модификации. Если
    ресурса нет — ошибка. Для директорий size — размер inode-блока ФС, не
    количество файлов; для содержимого директории — ls/tree.
    """
    try:
        meta = shell.meta(path)
    except WorkspaceNotFoundError as e:
        raise RuntimeError(f"Не найдено: {path}") from e
    except WorkspaceError as e:
        raise RuntimeError(f"Ошибка stat: {e}") from e
    return {
        "path": meta.path,
        "kind": meta.kind,
        "size": meta.size,
        "modified": meta.modified.isoformat(),
    }
