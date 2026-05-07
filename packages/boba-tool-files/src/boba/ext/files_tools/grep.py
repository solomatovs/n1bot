"""Tool: поиск по содержимому файлов (grep-like)."""

from __future__ import annotations

from dataclasses import dataclass
from itertools import islice
from typing import ClassVar

from boba.coercion import (
    ChainCoercer,
    Default,
    IsBool,
    IsInt,
    IsString,
    MinValue,
    NonEmpty,
    Nullable,
)
from boba.declaration import FieldSpec, ObjectSchema
from boba.plugin import ExtensionContext
from boba.plugin.prompt import PromptOverlay
from boba.tools.domain import (
    TextResult,
    Tool,
    ToolContext,
    ToolExecutionError,
    ToolId,
    ToolResult,
    ToolSourceId,
)
from boba.workspace import GrepMatch, WorkspaceError, WorkspaceNotFoundError

__all__ = ["GrepTool", "GrepToolConfig"]


@dataclass(frozen=True)
class GrepArgs:
    pattern: str
    path: str | None
    recursive: bool
    include: str | None
    case_insensitive: bool
    context: int
    limit: int
    fixed_string: bool


@dataclass(frozen=True)
class GrepToolConfig:
    prompt: PromptOverlay


class GrepTool(Tool[GrepArgs]):
    """Поиск подстроки/regex по содержимому файлов."""

    _ID: ClassVar[ToolId] = ToolId("grep")
    _SOURCE: ClassVar[ToolSourceId] = ToolSourceId("plugin.files")

    def __init__(self, cfg: GrepToolConfig, ctx: ExtensionContext) -> None:
        self._cfg = cfg
        self._ctx = ctx

    def tool_id(self) -> ToolId:
        return self._ID

    def tool_source_id(self) -> ToolSourceId:
        return self._SOURCE

    def definition(self) -> ObjectSchema[GrepArgs]:
        return self._cfg.prompt.apply(ObjectSchema(
            description=(
                "Найти совпадения pattern в текстовых файлах. Формат "
                "результата: 'path:line: content'. Бинарные и недекодируемые "
                "файлы пропускаются. При переполнении limit ответ обрезается "
                "с маркером."
            ),
            fields=[
                FieldSpec(
                    name="pattern",
                    description="Python-regex; литерал при fixed_string=true.",
                    coercer=ChainCoercer(IsString(), NonEmpty()),
                    required=True,
                ),
                FieldSpec(
                    name="path",
                    description="Стартовый путь. Без значения — cwd.",
                    coercer=Nullable(ChainCoercer(IsString(), NonEmpty())),
                ),
                FieldSpec(
                    name="recursive",
                    description="Рекурсивный обход директории. По умолчанию true.",
                    coercer=ChainCoercer(Default(True), IsBool()),
                ),
                FieldSpec(
                    name="include",
                    description=(
                        "Fnmatch-glob по пути (например '*.py'). "
                        "Без значения — все файлы."
                    ),
                    coercer=Nullable(ChainCoercer(IsString(), NonEmpty())),
                ),
                FieldSpec(
                    name="case_insensitive",
                    description="Игнорировать регистр. По умолчанию false.",
                    coercer=ChainCoercer(Default(False), IsBool()),
                ),
                FieldSpec(
                    name="context",
                    description=(
                        "Строк контекста до и после каждого совпадения. "
                        "По умолчанию 0."
                    ),
                    coercer=ChainCoercer(Default(0), IsInt(), MinValue(0)),
                ),
                FieldSpec(
                    name="limit",
                    description="Максимум совпадений в ответе. По умолчанию 100.",
                    coercer=ChainCoercer(Default(100), IsInt(), MinValue(1)),
                ),
                FieldSpec(
                    name="fixed_string",
                    description="Литеральный поиск без regex. По умолчанию false.",
                    coercer=ChainCoercer(Default(False), IsBool()),
                ),
            ],
            factory=GrepArgs,
        ))

    def execute(self, ctx: ToolContext, req: GrepArgs) -> ToolResult:
        try:
            iterator = ctx.project_workspace.grep(
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
                tool_id=self._ID,
                message=f"Путь не найден: {req.path}",
            ) from e
        except WorkspaceError as e:
            raise ToolExecutionError(
                tool_id=self._ID,
                message=f"Ошибка grep: {e}",
            ) from e

        truncated = len(matches) > req.limit
        if truncated:
            matches = matches[: req.limit]

        if not matches:
            return TextResult(text="Совпадений не найдено.")

        body = self._format_matches(matches, req.context)
        footer = f"\n\n{len(matches)} совпадение(й)"
        if truncated:
            footer += f" (truncated at limit={req.limit})"
        return TextResult(text=body + footer)

    @staticmethod
    def _format_matches(matches: list[GrepMatch], context: int) -> str:
        parts: list[str] = []
        for i, m in enumerate(matches):
            if context > 0 and i > 0:
                parts.append("--")
            for j, ctx_line in enumerate(m.before):
                n = m.line - len(m.before) + j
                parts.append(f"{m.path}:{n}- {ctx_line}")
            parts.append(f"{m.path}:{m.line}: {m.content}")
            for j, ctx_line in enumerate(m.after):
                n = m.line + 1 + j
                parts.append(f"{m.path}:{n}- {ctx_line}")
        return "\n".join(parts)
