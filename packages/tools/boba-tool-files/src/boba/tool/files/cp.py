"""Tool: копирование файла или директории (cp / cp -r)."""

from __future__ import annotations

from typing import Annotated

from pydantic import Field

from boba.tools import FromDI, Scope, tool
from boba.workspace.contract import (
    ProjectWorkspaceShell,
    WorkspaceError,
    WorkspaceNotFoundError,
)

__all__ = ["cp"]


@tool
def cp(
    src: Annotated[str, Field(min_length=1, description="Путь источника.")],
    dst: Annotated[str, Field(min_length=1, description="Путь назначения.")],
    shell: Annotated[ProjectWorkspaceShell, FromDI(Scope.APP)],
    recursive: Annotated[
        bool,
        Field(description="Рекурсивное копирование директории. По умолчанию false."),
    ] = False,
) -> str:
    """Скопировать файл или директорию. Для директорий требуется recursive=true."""
    try:
        shell.copy(src, dst, recursive=recursive)
    except WorkspaceNotFoundError as e:
        raise RuntimeError(f"Источник не найден: {src}") from e
    except WorkspaceError as e:
        raise RuntimeError(f"Ошибка копирования: {e}") from e
    return f"Скопировано: {src} → {dst}"
