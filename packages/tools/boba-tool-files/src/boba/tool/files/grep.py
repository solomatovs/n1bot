"""Tool: поиск по содержимому файлов (grep-like)."""

from __future__ import annotations

from dataclasses import dataclass
from itertools import islice

from pydantic import BaseModel, ConfigDict, Field

from boba.plugin.prompt import PromptOverlay
from boba.tool.files._base import FsToolBase
from boba.tools.domain import (
    JsonResult,
    ToolContext,
    ToolExecutionError,
    ToolResult,
)
from boba.workspace.contract import WorkspaceError, WorkspaceNotFoundError

__all__ = ["GrepArgs", "GrepTool", "GrepToolConfig"]


class GrepArgs(BaseModel):
    """Найти совпадения pattern в текстовых файлах.

    Формат результата: 'path:line: content'. Бинарные и недекодируемые файлы
    пропускаются. При переполнении limit ответ обрезается с маркером.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    pattern: str = Field(
        min_length=1,
        description="Python-regex; литерал при fixed_string=true.",
    )
    path: str | None = Field(
        default=None,
        min_length=1,
        description="Стартовый путь. Без значения — cwd.",
    )
    recursive: bool = Field(
        default=True,
        description="Рекурсивный обход директории. По умолчанию true.",
    )
    include: str | None = Field(
        default=None,
        min_length=1,
        description=(
            "Fnmatch-glob по пути (например '*.py'). Без значения — все файлы."
        ),
    )
    case_insensitive: bool = Field(
        default=False,
        description="Игнорировать регистр. По умолчанию false.",
    )
    context: int = Field(
        default=0,
        ge=0,
        description="Строк контекста до и после каждого совпадения. По умолчанию 0.",
    )
    limit: int = Field(
        default=100,
        ge=1,
        description="Максимум совпадений в ответе. По умолчанию 100.",
    )
    fixed_string: bool = Field(
        default=False,
        description="Литеральный поиск без regex. По умолчанию false.",
    )


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
