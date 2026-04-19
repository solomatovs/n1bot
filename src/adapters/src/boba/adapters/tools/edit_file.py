"""Tool: запись/обновление содержимого файла в workspace."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from boba.domain.core.patterns import Converter
from boba.domain.core.tools import (
    JsonType,
    ParamSchema,
    Tool,
    ToolDefinition,
    ToolExecutionError,
    ToolId,
    ToolInputSchema,
    ToolResult,
    ToolSourceId,
)
from boba.domain.core.workspace import WorkspaceError, WorkspaceService


@dataclass(frozen=True)
class EditFileArgs:
    filename: str
    content: str


class EditFileArgsConverter(Converter[dict[str, Any], EditFileArgs]):
    def convert(self, value: dict[str, Any]) -> EditFileArgs:
        return EditFileArgs(
            filename=str(value["filename"]),
            content=str(value["content"]),
        )


class EditFileTool(Tool[EditFileArgs]):
    """Запись/обновление файла в workspace (перезапись целиком)."""

    _ID = ToolId("edit_file")
    _SOURCE = ToolSourceId("builtin.files")

    def __init__(self, workspace: WorkspaceService) -> None:
        self._workspace = workspace

    def tool_id(self) -> ToolId:
        return self._ID

    def tool_source_id(self) -> ToolSourceId:
        return self._SOURCE

    def args_converter(self) -> Converter[dict[str, Any], EditFileArgs]:
        return EditFileArgsConverter()

    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            description=(
                "Записать или перезаписать файл в workspace целиком. "
                "Создаёт файл, если его не было; иначе полностью заменяет содержимое."
            ),
            input_schema=ToolInputSchema(
                params=[
                    ParamSchema(
                        name="filename",
                        type=JsonType.STRING,
                        description="Путь к файлу внутри workspace",
                    ),
                    ParamSchema(
                        name="content",
                        type=JsonType.STRING,
                        description="Новое полное содержимое файла",
                    ),
                ]
            ),
        )

    def execute(self, ctx: None, args: EditFileArgs) -> ToolResult:
        existed = self._workspace.exists(args.filename)
        try:
            with self._workspace.write_text(args.filename) as f:
                f.write(args.content)
        except WorkspaceError as e:
            raise ToolExecutionError(
                tool_id=self._ID, message=f"Ошибка записи: {e}"
            ) from e

        action = "обновлён" if existed else "создан"
        return ToolResult(
            content=f"Файл {action}: {args.filename} ({len(args.content)} символов)"
        )
