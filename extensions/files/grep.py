"""Tool: поиск по содержимому файлов (grep-like)."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from itertools import islice
from typing import Any

from boba.adapters.tool_providers import StaticToolSource
from boba.domain.core.patterns import Converter
from boba.domain.core.tools import (
    ChainValidator,
    Default,
    IsBool,
    IsInt,
    IsString,
    MinValue,
    NonEmpty,
    ParamSchema,
    Pass,
    Required,
    Tool,
    ToolContext,
    ToolDefinition,
    ToolExecutionError,
    ToolId,
    ToolInputSchema,
    ToolResult,
    ToolSource,
    ToolSourceId,
)
from boba.domain.core.workspace import (
    GrepMatch,
    WorkspaceError,
    WorkspaceNotFoundError,
)
from boba.infra.extensions import ExtensionContext


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
            input_schema=ToolInputSchema(
                params=[
                    ParamSchema(
                        name="pattern",
                        description="Python-regex; литерал при fixed_string=true.",
                        validator=ChainValidator(Required(), IsString(), NonEmpty()),
                    ),
                    ParamSchema(
                        name="path",
                        description="Стартовый путь. Без значения — cwd.",
                        validator=ChainValidator(IsString(), NonEmpty()),
                    ),
                    ParamSchema(
                        name="recursive",
                        description="Рекурсивный обход директории. По умолчанию true.",
                        validator=ChainValidator(Default(True), IsBool()),
                    ),
                    ParamSchema(
                        name="include",
                        description=(
                            "Fnmatch-glob по пути (например '*.py'). "
                            "Без значения — все файлы."
                        ),
                        validator=ChainValidator(IsString(), NonEmpty()),
                    ),
                    ParamSchema(
                        name="case_insensitive",
                        description="Игнорировать регистр. По умолчанию false.",
                        validator=ChainValidator(Default(False), IsBool()),
                    ),
                    ParamSchema(
                        name="context",
                        description=(
                            "Строк контекста до и после каждого совпадения. "
                            "По умолчанию 0."
                        ),
                        validator=ChainValidator(
                            Default(0),
                            IsInt(),
                            MinValue(0),
                        ),
                    ),
                    ParamSchema(
                        name="limit",
                        description="Максимум совпадений в ответе. По умолчанию 100.",
                        validator=ChainValidator(
                            Default(100),
                            IsInt(),
                            MinValue(1),
                        ),
                    ),
                    ParamSchema(
                        name="fixed_string",
                        description="Литеральный поиск без regex. По умолчанию false.",
                        validator=ChainValidator(Default(False), IsBool()),
                    ),
                ],
                invariants=Pass(),
            ),
        )

    def execute(self, ctx: ToolContext, args: GrepArgs) -> ToolResult:
        try:
            iterator = ctx.project_workspace.grep(
                args.pattern,
                args.path,
                recursive=args.recursive,
                include=args.include,
                case_insensitive=args.case_insensitive,
                context=args.context,
                limit=args.limit + 1,  # +1 чтобы заметить, что упёрлись в потолок
                fixed_string=args.fixed_string,
            )
            matches = list(islice(iterator, args.limit + 1))
        except WorkspaceNotFoundError as e:
            raise ToolExecutionError(
                tool_id=self._ID,
                message=f"Путь не найден: {args.path}",
            ) from e
        except WorkspaceError as e:
            raise ToolExecutionError(
                tool_id=self._ID,
                message=f"Ошибка grep: {e}",
            ) from e

        truncated = len(matches) > args.limit
        if truncated:
            matches = matches[: args.limit]

        if not matches:
            return ToolResult(content="Совпадений не найдено.")

        body = self._format_matches(matches, args.context)
        footer = f"\n\n{len(matches)} совпадение(й)"
        if truncated:
            footer += f" (truncated at limit={args.limit})"
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

def register_tools(ctx: ExtensionContext) -> Iterable[ToolSource]:
    yield StaticToolSource(
        ToolSourceId("builtin.files.grep"),
        priority=0,
        tools=[GrepTool()],
    )
