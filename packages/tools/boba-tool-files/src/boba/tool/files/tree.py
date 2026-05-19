"""Tool: рекурсивный обход workspace."""

from __future__ import annotations

from itertools import islice
from typing import Annotated, Any

from pydantic import Field

from boba.tool.files.enable import files_enable_if
from boba.tools import FromDI, Scope, tool
from boba.workspace.contract import ProjectWorkspaceShell, WorkspaceError

__all__ = ["TreeTool"]


@tool(enable_if=files_enable_if("tree"))
class TreeTool:
    """Рекурсивный обход всех файлов workspace.

    Плоский список путей. При переполнении limit ответ обрезается с маркером.
    Для одного уровня — ls.
    """

    def __call__(
        self,
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
        try:
            iterator = shell.tree(path)
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
