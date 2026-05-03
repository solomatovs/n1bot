"""Tool: поиск по содержимому файлов (grep-like)."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
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
    ParseString,
)
from boba.config.section import ConfigSection
from boba.declaration import FieldSpec, ObjectSchema
from boba.rendering import TextResult
from boba.tools import (
    ParamOverlay,
    Tool,
    ToolContext,
    ToolExecutionError,
    ToolId,
    ToolResult,
    ToolSourceId,
    param_desc,
    params_field,
)
from boba.workspace import (
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


@dataclass(frozen=True)
class GrepToolConfig:
    """DTO секции [ext.files.tools.grep]."""

    description: str
    params: Mapping[str, ParamOverlay] = field(default_factory=dict)


class GrepTool(Tool[GrepArgs]):
    """Поиск подстроки/regex по содержимому файлов."""

    _ID = ToolId("grep")
    _SOURCE = ToolSourceId("builtin.files")

    DEFAULT_DESCRIPTION: ClassVar[str] = (
        "Найти совпадения pattern в текстовых файлах. Формат "
        "результата: 'path:line: content'. Бинарные и недекодируемые "
        "файлы пропускаются. При переполнении limit ответ обрезается "
        "с маркером."
    )
    DEFAULT_PATTERN_DESC: ClassVar[str] = "Python-regex; литерал при fixed_string=true."
    DEFAULT_PATH_DESC: ClassVar[str] = "Стартовый путь. Без значения — cwd."
    DEFAULT_RECURSIVE_DESC: ClassVar[str] = (
        "Рекурсивный обход директории. По умолчанию true."
    )
    DEFAULT_INCLUDE_DESC: ClassVar[str] = (
        "Fnmatch-glob по пути (например '*.py'). Без значения — все файлы."
    )
    DEFAULT_CASE_INSENSITIVE_DESC: ClassVar[str] = (
        "Игнорировать регистр. По умолчанию false."
    )
    DEFAULT_CONTEXT_DESC: ClassVar[str] = (
        "Строк контекста до и после каждого совпадения. По умолчанию 0."
    )
    DEFAULT_LIMIT_DESC: ClassVar[str] = (
        "Максимум совпадений в ответе. По умолчанию 100."
    )
    DEFAULT_FIXED_STRING_DESC: ClassVar[str] = (
        "Литеральный поиск без regex. По умолчанию false."
    )

    def __init__(self, cfg: GrepToolConfig) -> None:
        self._cfg = cfg

    def tool_id(self) -> ToolId:
        return self._ID

    def tool_source_id(self) -> ToolSourceId:
        return self._SOURCE

    def definition(self) -> ObjectSchema[GrepArgs]:
        p = self._cfg.params
        return ObjectSchema(
            description=self._cfg.description,
            fields=[
                FieldSpec(
                    name="pattern",
                    description=param_desc(
                        p, "pattern", self.DEFAULT_PATTERN_DESC
                    ),
                    coercer=ChainCoercer(IsString(), NonEmpty()),
                    required=True,
                ),
                FieldSpec(
                    name="path",
                    description=param_desc(p, "path", self.DEFAULT_PATH_DESC),
                    coercer=Nullable(ChainCoercer(IsString(), NonEmpty())),
                ),
                FieldSpec(
                    name="recursive",
                    description=param_desc(
                        p, "recursive", self.DEFAULT_RECURSIVE_DESC
                    ),
                    coercer=ChainCoercer(Default(True), IsBool()),
                ),
                FieldSpec(
                    name="include",
                    description=param_desc(
                        p, "include", self.DEFAULT_INCLUDE_DESC
                    ),
                    coercer=Nullable(ChainCoercer(IsString(), NonEmpty())),
                ),
                FieldSpec(
                    name="case_insensitive",
                    description=param_desc(
                        p, "case_insensitive", self.DEFAULT_CASE_INSENSITIVE_DESC
                    ),
                    coercer=ChainCoercer(Default(False), IsBool()),
                ),
                FieldSpec(
                    name="context",
                    description=param_desc(
                        p, "context", self.DEFAULT_CONTEXT_DESC
                    ),
                    coercer=ChainCoercer(
                        Default(0),
                        IsInt(),
                        MinValue(0),
                    ),
                ),
                FieldSpec(
                    name="limit",
                    description=param_desc(p, "limit", self.DEFAULT_LIMIT_DESC),
                    coercer=ChainCoercer(
                        Default(100),
                        IsInt(),
                        MinValue(1),
                    ),
                ),
                FieldSpec(
                    name="fixed_string",
                    description=param_desc(
                        p, "fixed_string", self.DEFAULT_FIXED_STRING_DESC
                    ),
                    coercer=ChainCoercer(Default(False), IsBool()),
                ),
            ],
            factory=GrepArgs,
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


class GrepToolSection(ConfigSection[GrepToolConfig]):
    """Секция [ext.files.tools.grep]."""

    namespace: ClassVar[tuple[str, ...]] = ("ext", "files", "tools", "grep")

    schema: ClassVar[ObjectSchema[GrepToolConfig]] = ObjectSchema(
        description="Конфиг tool 'grep'.",
        fields=[
            FieldSpec(
                name="description",
                coercer=ChainCoercer(
                    Default(GrepTool.DEFAULT_DESCRIPTION), ParseString()
                ),
                description="Override описания tool'а; пусто — дефолт из кода.",
            ),
            params_field("params"),
        ],
        factory=GrepToolConfig,
    )
