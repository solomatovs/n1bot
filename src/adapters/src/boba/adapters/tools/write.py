"""Tool: перезаписать файл целиком."""

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
class WriteArgs:
    filename: str
    content: str
    encoding: str


class WriteArgsConverter(Converter[dict[str, Any], WriteArgs]):
    def convert(self, value: dict[str, Any]) -> WriteArgs:
        return WriteArgs(
            filename=value["filename"],
            content=value["content"],
            encoding=value["encoding"],
        )


class WriteTool(Tool[WriteArgs]):
    """Полностью перезаписать файл содержимым."""

    _ID = ToolId("write")
    _SOURCE = ToolSourceId("builtin.files")

    def __init__(self, workspace: ProjectWorkspaceShell) -> None:
        self._workspace = workspace

    def tool_id(self) -> ToolId:
        return self._ID

    def tool_source_id(self) -> ToolSourceId:
        return self._SOURCE

    def typed_args_converter(self) -> Converter[dict[str, Any], WriteArgs]:
        return WriteArgsConverter()

    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            description=(
                "Полностью перезаписать файл указанным содержимым. Если "
                "файла или промежуточных директорий нет — они создаются. "
                "Для точечной правки фрагмента используй edit; для "
                "дозаписи в конец — append."
            ),
            input_schema=ToolInputSchema(
                params=[
                    ParamSchema(
                        name="filename",
                        description="Путь к файлу.",
                        validator=ChainValidator(Required(), IsString(), NonEmpty()),
                    ),
                    ParamSchema(
                        name="content",
                        description="Полное новое содержимое файла.",
                        validator=ChainValidator(Required(), IsString()),
                    ),
                    ParamSchema(
                        name="encoding",
                        description=(
                            "Текстовая кодировка, например 'utf-8' или "
                            "'cp1251'. По умолчанию — 'utf-8'."
                        ),
                        validator=ChainValidator(
                            Default("utf-8"), IsString(), NonEmpty(),
                        ),
                    ),
                ],
                invariants=Pass(),
            ),
        )

    def execute(self, ctx: None, args: WriteArgs) -> ToolResult:
        existed = self._workspace.exists(args.filename)
        try:
            with self._workspace.write_text(args.filename, args.encoding) as f:
                f.write(args.content)
        except WorkspaceError as e:
            raise ToolExecutionError(
                tool_id=self._ID, message=f"Ошибка записи: {e}",
            ) from e
        action = "обновлён" if existed else "создан"
        return ToolResult(
            content=f"Файл {action}: {args.filename} ({len(args.content)} символов)",
        )
