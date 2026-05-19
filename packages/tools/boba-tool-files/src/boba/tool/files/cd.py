"""Tool: сменить текущую директорию."""

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

__all__ = ["cd"]


@tool(enable_if=files_enable_if("cd"))
def cd(
    path: Annotated[str, Field(min_length=1, description="Путь директории.")],
    shell: Annotated[ProjectWorkspaceShell, FromDI(Scope.APP)],
) -> str:
    """Сменить текущую директорию."""
    try:
        shell.cd(path)
    except WorkspaceNotFoundError as e:
        raise RuntimeError(f"Директория не найдена: {path}") from e
    except WorkspaceError as e:
        raise RuntimeError(f"Ошибка cd: {e}") from e
    return f"Текущая директория: {shell.cwd}"
