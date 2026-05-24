"""Tool: рекурсивный обход workspace."""

from __future__ import annotations

from itertools import islice
from typing import Annotated, Any

from pydantic import Field

from boba.tools import FromDI, Scope, tool
from boba.workspace.contract import ProjectWorkspaceShell, WorkspaceError

__all__ = ["tree"]


@tool
def tree(
    limit: Annotated[int, Field(ge=1, description="Максимум путей в ответе.")],
    shell: Annotated[ProjectWorkspaceShell, FromDI(Scope.APP)],
    path: Annotated[
        str | None,
        Field(
            min_length=1,
            description="Корень обхода. Без значения — корень workspace.",
        ),
    ] = None,
) -> dict[str, Any]:
    """
    Рекурсивный обход всех элементов папки

    Включает файлы и директории; каждый item — {path, kind}
    Для одного уровня используй ls.
    """
    try:
        iterator = shell.tree(path)
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
