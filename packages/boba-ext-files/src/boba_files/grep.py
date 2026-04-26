"""Tool: поиск по содержимому файлов (grep-like)."""

from __future__ import annotations

from dataclasses import dataclass
from itertools import islice
from typing import Any

from boba.domain.core.patterns import Converter
from boba.domain.core.tools import (
    ChainConverter,
    Default,
    IsBool,
    IsInt,
    IsString,
    MinValue,
    NonEmpty,
    FieldSpec,
    Pass,
    Required,
    Tool,
    ToolContext,
    ToolDefinition,
    ToolExecutionError,
    ToolId,
    ObjectSchema,
    ToolResult,
    ToolSourceId,
)
from boba.domain.core.workspace import (
    GrepMatch,
    WorkspaceError,
    WorkspaceNotFoundError,
)


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


class GrepArgsConverter(Converter[dict[str, Any], GrepArgs]):
    def convert(self, value: dict[str, Any]) -> GrepArgs:
        return GrepArgs(
            pattern=value["pattern"],
            path=value.get("path"),
            recursive=value["recursive"],
            include=value.get("include"),
            case_insensitive=value["case_insensitive"],
            context=value["context"],
            limit=value["limit"],
            fixed_string=value["fixed_string"],
        )


class GrepTool(Tool[GrepArgs]):
    """Поиск подстроки/regex по содержимому файлов."""

    _ID = ToolId("grep")
    _SOURCE = ToolSourceId("builtin.files")

    def tool_id(self) -> ToolId:
        return self._ID

    def tool_source_id(self) -> ToolSourceId:
        return self._SOURCE

    def typed_args_converter(self) -> Converter[dict[str, Any], GrepArgs]:
        return GrepArgsConverter()

    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            description=(
                "Найти совпадения pattern в текстовых файлах. Формат "
                "результата: 'path:line: content'. Бинарные и недекодируемые "
                "файлы пропускаются. При переполнении limit ответ обрезается "
                "с маркером."
            ),
            input_schema=ObjectSchema(
                fields=[
                    FieldSpec(
                        name="pattern",
                        description="Python-regex; литерал при fixed_string=true.",
                        converter=ChainConverter(Required(), IsString(), NonEmpty()),
                    ),
                    FieldSpec(
                        name="path",
                        description="Стартовый путь. Без значения — cwd.",
                        converter=ChainConverter(IsString(), NonEmpty()),
                    ),
                    FieldSpec(
                        name="recursive",
                        description="Рекурсивный обход директории. По умолчанию true.",
                        converter=ChainConverter(Default(True), IsBool()),
                    ),
                    FieldSpec(
                        name="include",
                        description=(
                            "Fnmatch-glob по пути (например '*.py'). "
                            "Без значения — все файлы."
                        ),
                        converter=ChainConverter(IsString(), NonEmpty()),
                    ),
                    FieldSpec(
                        name="case_insensitive",
                        description="Игнорировать регистр. По умолчанию false.",
                        converter=ChainConverter(Default(False), IsBool()),
                    ),
                    FieldSpec(
                        name="context",
                        description=(
                            "Строк контекста до и после каждого совпадения. "
                            "По умолчанию 0."
                        ),
                        converter=ChainConverter(
                            Default(0),
                            IsInt(),
                            MinValue(0),
                        ),
                    ),
                    FieldSpec(
                        name="limit",
                        description="Максимум совпадений в ответе. По умолчанию 100.",
                        converter=ChainConverter(
                            Default(100),
                            IsInt(),
                            MinValue(1),
                        ),
                    ),
                    FieldSpec(
                        name="fixed_string",
                        description="Литеральный поиск без regex. По умолчанию false.",
                        converter=ChainConverter(Default(False), IsBool()),
                    ),
                ],
                invariants=Pass(),
            ),
        )

    def execute(self, ctx: ToolContext, req: GrepArgs) -> ToolResult:
        try:
            iterator = ctx.project_workspace.grep(
                req.pattern,
                req.path,
                recursive=req.recursive,
                include=req.include,
                case_insensitive=req.case_insensitive,
                context=req.context,
                limit=req.limit + 1,  # +1 чтобы заметить, что упёрлись в потолок
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
            return ToolResult(content="Совпадений не найдено.")

        body = self._format_matches(matches, req.context)
        footer = f"\n\n{len(matches)} совпадение(й)"
        if truncated:
            footer += f" (truncated at limit={req.limit})"
        return ToolResult(content=body + footer)

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

