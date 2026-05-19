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
    """Плоский список элементов workspace (без рекурсии).

    При переполнении limit ответ обрезается с маркером.
    Для рекурсии — tree.
    """
    try:
        iterator = shell.ls(path)
        items = list(islice(iterator, limit + 1))
    except WorkspaceError as e:
        raise RuntimeError(f"Ошибка обхода: {e}") from e

    truncated = len(items) > limit
    if truncated:
        items = items[:limit]

    return {
        "location": path or "/",
        "items": items,
        "count": len(items),
        "truncated": truncated,
        "limit": limit,
    }
