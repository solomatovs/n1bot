"""Tool: поиск по содержимому файлов (grep-like)."""

from __future__ import annotations

from itertools import islice
from typing import Annotated, Any

from pydantic import Field

from boba.tool.files.enable import files_enable_if
from boba.tools import FromDI, Scope, tool
from boba.workspace.contract import (
    ProjectWorkspaceShell,
    WorkspaceError,
    WorkspaceNotFoundError,
)

__all__ = ["GrepTool"]


@tool(enable_if=files_enable_if("grep"))
class GrepTool:
    """Найти совпадения pattern в текстовых файлах.

    Формат результата: 'path:line: content'. Бинарные и недекодируемые файлы
    пропускаются. При переполнении limit ответ обрезается с маркером.
    """

    def __call__(
        self,
        pattern: Annotated[
            str,
            Field(min_length=1, description="Python-regex; литерал при fixed_string=true."),
        ],
        shell: Annotated[ProjectWorkspaceShell, FromDI(Scope.APP)],
        path: Annotated[
            str | None,
            Field(min_length=1, description="Стартовый путь. Без значения — cwd."),
        ] = None,
        recursive: Annotated[
            bool,
            Field(description="Рекурсивный обход директории. По умолчанию true."),
        ] = True,
        include: Annotated[
            str | None,
            Field(
                min_length=1,
                description="Fnmatch-glob по пути (например '*.py'). Без значения — все.",
            ),
        ] = None,
        case_insensitive: Annotated[
            bool, Field(description="Игнорировать регистр. По умолчанию false."),
        ] = False,
        context: Annotated[
            int,
            Field(ge=0, description="Строк контекста до и после каждого совпадения."),
        ] = 0,
        limit: Annotated[
            int,
            Field(ge=1, description="Максимум совпадений в ответе. По умолчанию 100."),
        ] = 100,
        fixed_string: Annotated[
            bool,
            Field(description="Литеральный поиск без regex. По умолчанию false."),
        ] = False,
    ) -> dict[str, Any]:
        try:
            iterator = shell.grep(
                pattern,
                path,
                recursive=recursive,
                include=include,
                case_insensitive=case_insensitive,
                context=context,
                limit=limit,
                fixed_string=fixed_string,
            )
            matches = list(islice(iterator, limit + 1))
        except WorkspaceNotFoundError as e:
            raise RuntimeError(f"Путь не найден: {path}") from e
        except WorkspaceError as e:
            raise RuntimeError(f"Ошибка grep: {e}") from e

        total = len(matches)
        truncated = total > limit
        if truncated:
            matches = matches[:limit]

        return {
            "matches": [
                {
                    "path": m.path,
                    "line": m.line,
                    "content": m.content,
                    "before": list(m.before),
                    "after": list(m.after),
                }
                for m in matches
            ],
            "count": len(matches),
            "total": total,
            "truncated": truncated,
            "limit": limit,
        }
