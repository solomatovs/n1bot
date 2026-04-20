"""Tool: удаление файла из пользовательского workspace."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from boba.domain.core.patterns import Converter
from boba.domain.core.tools import (
    ChainValidator,
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
    UserWorkspaceService,
    WorkspaceError,
    WorkspaceNotFoundError,
)


@dataclass(frozen=True)
class DeleteFileArgs:
    filename: str


class DeleteFileArgsConverter(Converter[dict[str, Any], DeleteFileArgs]):
    """Маппит провалидированный dict в :class:`DeleteFileArgs`."""

    def convert(self, value: dict[str, Any]) -> DeleteFileArgs:
        return DeleteFileArgs(filename=value["filename"])


class DeleteFileTool(Tool[DeleteFileArgs]):
    """Удаление файла из workspace."""

    _ID = ToolId("delete_file")
    _SOURCE = ToolSourceId("builtin.files")

    def __init__(self, workspace: UserWorkspaceService) -> None:
        self._workspace = workspace

    def tool_id(self) -> ToolId:
        return self._ID

    def tool_source_id(self) -> ToolSourceId:
        return self._SOURCE

    def typed_args_converter(self) -> Converter[dict[str, Any], DeleteFileArgs]:
        return DeleteFileArgsConverter()

    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            description=(
                "Удалить файл. Операция безвозвратна — отмены нет, используй "
                "с осторожностью. Если файла нет — возвращает ошибку 'Файл "
                "не найден'."
            ),
            input_schema=ToolInputSchema(
                params=[
                    ParamSchema(
                        name="filename",
                        description="Путь к файлу.",
                        validator=ChainValidator(Required(), IsString(), NonEmpty()),
                    ),
                ],
                invariants=Pass(),
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
