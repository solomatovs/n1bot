"""Tool: создать директорию."""

from __future__ import annotations

from typing import Annotated

from pydantic import Field

from boba.tool.files.enable import files_enable_if
from boba.tools import FromDI, Scope, tool
from boba.workspace.contract import ProjectWorkspaceShell, WorkspaceError

__all__ = ["MkdirTool"]


@tool(enable_if=files_enable_if("mkdir"))
class MkdirTool:
    """Создать директорию (включая промежуточные).

    Если уже существует — no-op. Если по пути файл — ошибка.
    """

    def __call__(
        self,
        path: Annotated[
            str, Field(min_length=1, description="Путь создаваемой директории."),
        ],
        shell: Annotated[ProjectWorkspaceShell, FromDI(Scope.APP)],
    ) -> str:
        try:
            shell.mkdir(path)
        except WorkspaceError as e:
            raise RuntimeError(f"Ошибка mkdir: {e}") from e
        return f"Директория создана: {path}"
