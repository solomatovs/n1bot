"""Tool: показать текущую директорию."""

from __future__ import annotations

from typing import Annotated

from boba.tool.files.enable import files_enable_if
from boba.tools import FromDI, Scope, tool
from boba.workspace.contract import ProjectWorkspaceShell

__all__ = ["PwdTool"]


@tool(enable_if=files_enable_if("pwd"))
class PwdTool:
    """Возвращает путь текущей директории."""

    def __call__(
        self,
        shell: Annotated[ProjectWorkspaceShell, FromDI(Scope.APP)],
    ) -> str:
        return shell.cwd
