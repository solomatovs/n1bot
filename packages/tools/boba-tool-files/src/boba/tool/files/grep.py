"""Tool: поиск по содержимому файлов (grep-like)."""

from __future__ import annotations

from dataclasses import dataclass
from itertools import islice
from typing import Annotated

from boba.plugin.prompt import PromptOverlay
from boba.schema.coercion import MinValue, NonEmpty
from boba.tool.files._base import FsToolBase
from boba.tools.domain import (
    JsonResult,
    ToolContext,
    ToolExecutionError,
    ToolResult,
)
from boba.workspace.contract import WorkspaceError, WorkspaceNotFoundError

__all__ = ["GrepArgs", "GrepTool", "GrepToolConfig"]


@dataclass(frozen=True)
class GrepArgs:
    """Найти совпадения pattern в текстовых файлах.

    Формат результата: 'path:line: content'. Бинарные и недекодируемые файлы
    пропускаются. При переполнении limit ответ обрезается с маркером.
    """

    pattern: Annotated[
        str, "Python-regex; литерал при fixed_string=true.", NonEmpty()
    ]
    path: Annotated[
        str | None, "Стартовый путь. Без значения — cwd.", NonEmpty()
    ] = None
    recursive: Annotated[
        bool, "Рекурсивный обход директории. По умолчанию true."
    ] = True
    include: Annotated[
        str | None,
        "Fnmatch-glob по пути (например '*.py'). Без значения — все файлы.",
        NonEmpty(),
    ] = None
    case_insensitive: Annotated[
        bool, "Игнорировать регистр. По умолчанию false."
    ] = False
    context: Annotated[
        int,
        "Строк контекста до и после каждого совпадения. По умолчанию 0.",
        MinValue(0),
    ] = 0
    limit: Annotated[
        int, "Максимум совпадений в ответе. По умолчанию 100.", MinValue(1)
    ] = 100
    fixed_string: Annotated[
        bool, "Литеральный поиск без regex. По умолчанию false."
    ] = False


@dataclass(frozen=True)
class GrepToolConfig:
    prompt: PromptOverlay


class GrepTool(FsToolBase[GrepArgs, GrepToolConfig]):
    """Поиск подстроки/regex по содержимому файлов."""

    def execute(self, ctx: ToolContext, req: GrepArgs) -> ToolResult:
        try:
            iterator = self._shell.grep(
                req.pattern,
                req.path,
                recursive=req.recursive,
                include=req.include,
                case_insensitive=req.case_insensitive,
                context=req.context,
                limit=req.limit + 1,
                fixed_string=req.fixed_string,
            )
            matches = list(islice(iterator, req.limit + 1))
        except WorkspaceNotFoundError as e:
            raise ToolExecutionError(
                tool_id=self.tool_id(),
                message=f"Путь не найден: {req.path}",
            ) from e
        except WorkspaceError as e:
            raise ToolExecutionError(
                tool_id=self.tool_id(),
                message=f"Ошибка grep: {e}",
            ) from e

        total = len(matches)
        truncated = total > req.limit
        if truncated:
            matches = matches[: req.limit]

        return JsonResult(payload={
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
            "limit": req.limit,
        })
