"""Tool: переместить/переименовать файл или директорию."""

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

__all__ = ["mv"]


@tool(enable_if=files_enable_if("mv"))
def mv(
    src: Annotated[str, Field(min_length=1, description="Путь источника.")],
    dst: Annotated[str, Field(min_length=1, description="Путь назначения.")],
    shell: Annotated[ProjectWorkspaceShell, FromDI(Scope.APP)],
) -> str:
    """Переместить/переименовать файл или директорию.

    Если dst — существующая директория, src переносится внутрь. Файл по пути
    dst перезаписывается. Промежуточные директории не создаются.
    """
    try:
        shell.move(src, dst)
    except WorkspaceNotFoundError as e:
        raise RuntimeError(f"Источник не найден: {src}") from e
    except WorkspaceError as e:
        raise RuntimeError(f"Ошибка перемещения: {e}") from e
    return f"Перемещено: {src} → {dst}"
