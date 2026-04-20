"""Tool: поиск по содержимому файлов (grep-like)."""

from __future__ import annotations

from dataclasses import dataclass
from itertools import islice
from typing import Any

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
    ToolDefinition,
    ToolExecutionError,
    ToolId,
    ToolInputSchema,
    ToolResult,
    ToolSourceId,
)
from boba.domain.core.workspace import (
    GrepMatch,
    ProjectWorkspaceShell,
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

    def __init__(self, workspace: ProjectWorkspaceShell) -> None:
        self._workspace = workspace

    def tool_id(self) -> ToolId:
        return self._ID

    def tool_source_id(self) -> ToolSourceId:
        return self._SOURCE

    def typed_args_converter(self) -> Converter[dict[str, Any], GrepArgs]:
        return GrepArgsConverter()

    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            description=(
                "Поиск по содержимому текстовых файлов. По умолчанию pattern "
                "интерпретируется как regex (Python-синтаксис). Для "
                "литерального поиска выставь fixed_string=true. Бинарные и "
                "не декодируемые файлы пропускаются молча. Результат — "
                "список совпадений в формате 'path:line: content'; при "
                "context>0 добавляются строки до/после. Если результат "
                "урезан по limit — будет явный маркер."
            ),
            input_schema=ToolInputSchema(
                params=[
                    ParamSchema(
                        name="pattern",
                        description=(
                            "Regex (Python) или литерал при fixed_string=true."
                        ),
                        validator=ChainValidator(Required(), IsString(), NonEmpty()),
                    ),
                    ParamSchema(
                        name="path",
                        description=(
                            "Стартовый путь (файл или директория). Без "
                            "параметра — от текущей cwd."
                        ),
                        validator=ChainValidator(IsString(), NonEmpty()),
                    ),
                    ParamSchema(
                        name="recursive",
                        description=(
                            "Рекурсивный обход директории. По умолчанию true."
                        ),
                        validator=ChainValidator(Default(True), IsBool()),
                    ),
                    ParamSchema(
                        name="include",
                        description=(
                            "Fnmatch-glob по относительному пути "
                            "(например '*.py'). Без параметра — все файлы."
                        ),
                        validator=ChainValidator(IsString(), NonEmpty()),
                    ),
                    ParamSchema(
                        name="case_insensitive",
                        description="Регистронезависимый поиск. По умолчанию false.",
                        validator=ChainValidator(Default(False), IsBool()),
                    ),
                    ParamSchema(
                        name="context",
                        description=(
                            "Сколько строк контекста показывать до и после "
                            "каждого совпадения. По умолчанию 0."
                        ),
                        validator=ChainValidator(
                            Default(0), IsInt(), MinValue(0),
                        ),
                    ),
                    ParamSchema(
                        name="limit",
                        description=(
                            "Максимум совпадений в ответе (целое >= 1). По "
                            "умолчанию 100."
                        ),
                        validator=ChainValidator(
                            Default(100), IsInt(), MinValue(1),
                        ),
                    ),
                    ParamSchema(
                        name="fixed_string",
                        description=(
                            "Если true — pattern литеральная строка "
                            "(спецсимволы regex экранируются). По умолчанию "
                            "false."
                        ),
                        validator=ChainValidator(Default(False), IsBool()),
                    ),
                ],
                invariants=Pass(),
            ),
        )

    def execute(self, ctx: None, args: GrepArgs) -> ToolResult:
        try:
            iterator = self._workspace.grep(
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
                tool_id=self._ID, message=f"Путь не найден: {args.path}",
            ) from e
        except WorkspaceError as e:
            raise ToolExecutionError(
                tool_id=self._ID, message=f"Ошибка grep: {e}",
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
