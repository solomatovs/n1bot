"""Tool: показать текущую директорию."""

from __future__ import annotations

from typing import Annotated

from boba.tools import FromDI, Scope, tool
from boba.workspace.contract import ProjectWorkspaceShell

__all__ = ["pwd"]


@tool
def pwd(
    shell: Annotated[ProjectWorkspaceShell, FromDI(Scope.APP)],
) -> str:
    """Возвращает путь текущей директории."""
    return shell.cwd
