"""Tool: список элементов workspace без рекурсии."""

from __future__ import annotations

from itertools import islice
from typing import Annotated, Any

from pydantic import Field

from boba.tools import FromDI, Scope, tool
from boba.workspace.contract import ProjectWorkspaceShell, WorkspaceError

__all__ = ["ls"]


@tool
def ls(
    shell: Annotated[ProjectWorkspaceShell, FromDI(Scope.APP)],
    path: Annotated[
        str | None,
        Field(
            min_length=1,
            description="Путь директории. Без значения — корень workspace.",
        ),
    ] = None,
    limit: Annotated[
        int,
        Field(ge=1, description="Максимум элементов в ответе. По умолчанию 200."),
    ] = 200,
) -> dict[str, Any]:
    """
    Плоский список элементов workspace (без рекурсии).

    Включает файлы и директории; каждый item — {path, kind}
    Для рекурсивного обхода поддиректорий используй tree
    """
    try:
        iterator = shell.ls(path)
        entries = list(islice(iterator, limit + 1))
    except WorkspaceError as e:
        raise RuntimeError(f"Ошибка обхода: {e}") from e

    truncated = len(entries) > limit
    if truncated:
        entries = entries[:limit]

    return {
        "location": path or "/",
        "items": [{"path": e.path, "kind": e.kind} for e in entries],
        "count": len(entries),
        "truncated": truncated,
        "limit": limit,
    }
