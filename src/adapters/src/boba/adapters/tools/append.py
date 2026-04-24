"""Tool: дозаписать в конец файла."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from boba.domain.core.patterns import Converter
from boba.domain.core.tools import (
    ChainValidator,
    Default,
    IsString,
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
    ProjectWorkspaceShell,
    WorkspaceError,
)


@dataclass(frozen=True)
class AppendArgs:
    path: str
    content: str
    encoding: str


class AppendArgsConverter(Converter[dict[str, Any], AppendArgs]):
    def convert(self, value: dict[str, Any]) -> AppendArgs:
        return AppendArgs(
            path=value["path"],
            content=value["content"],
            encoding=value["encoding"],
        )


class AppendTool(Tool[AppendArgs]):
    """Дозаписать текст в конец файла."""

    _ID = ToolId("append")
    _SOURCE = ToolSourceId("builtin.files")

    def __init__(self, workspace: ProjectWorkspaceShell) -> None:
        self._workspace = workspace

    def tool_id(self) -> ToolId:
        return self._ID

    def tool_source_id(self) -> ToolSourceId:
        return self._SOURCE

    def typed_args_converter(self) -> Converter[dict[str, Any], AppendArgs]:
        return AppendArgsConverter()

    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            description=(
                "Дозаписать текст в конец файла. Если файла или "
                "промежуточных директорий нет — они создаются. Для "
                "полной перезаписи используй write; для точечной правки — "
                "edit."
            ),
            input_schema=ToolInputSchema(
                params=[
                    ParamSchema(
                        name="path",
                        description="Путь к файлу.",
                        validator=ChainValidator(Required(), IsString(), NonEmpty()),
                    ),
                    ParamSchema(
                        name="content",
                        description="Текст для добавления в конец файла.",
                        validator=ChainValidator(Required(), IsString()),
                    ),
                    ParamSchema(
                        name="encoding",
                        description=(
                            "Текстовая кодировка, например 'utf-8' или "
                            "'cp1251'. По умолчанию — 'utf-8'."
                        ),
                        validator=ChainValidator(
                            Default("utf-8"),
                            IsString(),
                            NonEmpty(),
                        ),
                    ),
                ],
                invariants=Pass(),
            ),
        )

    def execute(self, ctx: None, args: AppendArgs) -> ToolResult:
        existed = self._workspace.exists(args.path)
        try:
            with self._workspace.append_text(args.path, args.encoding) as f:
                f.write(args.content)
        except WorkspaceError as e:
            raise ToolExecutionError(
                tool_id=self._ID,
                message=f"Ошибка записи: {e}",
            ) from e
        action = "дозаписан" if existed else "создан"
        return ToolResult(
            content=f"Файл {action}: {args.path} ({len(args.content)} символов)",
        )
