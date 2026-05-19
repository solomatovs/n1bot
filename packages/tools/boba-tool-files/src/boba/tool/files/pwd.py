"""Tool: показать текущую директорию."""

from __future__ import annotations

from typing import Annotated

from boba.tool.files.enable import files_enable_if
from boba.tools import FromDI, Scope, tool
from boba.workspace.contract import ProjectWorkspaceShell

__all__ = ["pwd"]


@tool(enable_if=files_enable_if("pwd"))
def pwd(
    shell: Annotated[ProjectWorkspaceShell, FromDI(Scope.APP)],
) -> str:
    """Возвращает путь текущей директории."""
    return shell.cwd
