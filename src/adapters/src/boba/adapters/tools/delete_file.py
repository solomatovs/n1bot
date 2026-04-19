"""Tool: удаление файла из workspace."""

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
from boba.domain.core.workspace import (
    WorkspaceError,
    WorkspaceNotFoundError,
    WorkspaceService,
)


@dataclass(frozen=True)
class DeleteFileArgs:
    filename: str


class DeleteFileArgsConverter(Converter[dict[str, Any], DeleteFileArgs]):
    def convert(self, value: dict[str, Any]) -> DeleteFileArgs:
        return DeleteFileArgs(filename=str(value["filename"]))


class DeleteFileTool(Tool[DeleteFileArgs]):
    """Удаление файла из workspace."""

    _ID = ToolId("delete_file")
    _SOURCE = ToolSourceId("builtin.files")

    def __init__(self, workspace: WorkspaceService) -> None:
        self._workspace = workspace

    def tool_id(self) -> ToolId:
        return self._ID

    def tool_source_id(self) -> ToolSourceId:
        return self._SOURCE

    def args_converter(self) -> Converter[dict[str, Any], DeleteFileArgs]:
        return DeleteFileArgsConverter()

    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            description="Удалить файл из workspace.",
            input_schema=ToolInputSchema(
                params=[
                    ParamSchema(
                        name="filename",
                        type=JsonType.STRING,
                        description="Путь к файлу внутри workspace",
                    ),
                ]
            ),
        )

    def execute(self, ctx: None, args: DeleteFileArgs) -> ToolResult:
        try:
            self._workspace.delete(args.filename)
        except WorkspaceNotFoundError as e:
            raise ToolExecutionError(
                tool_id=self._ID, message=f"Файл не найден: {args.filename}"
            ) from e
        except WorkspaceError as e:
            raise ToolExecutionError(
                tool_id=self._ID, message=f"Ошибка удаления: {e}"
            ) from e
        return ToolResult(content=f"Файл удалён: {args.filename}")
