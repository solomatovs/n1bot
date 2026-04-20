"""Tool: удаление файла из workspace."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from boba.adapters.tools._workspace_arg import workspace_tool_id
from boba.domain.core.patterns import Converter
from boba.domain.core.tools import (
    ParamSchema,
    Tool,
    ToolDefinition,
    ToolExecutionError,
    ToolId,
    ToolInputSchema,
    ToolResult,
    ToolSourceId,
)
from boba.domain.core.validation import (
    ChainValidator,
    IsString,
    NonEmpty,
    Required,
)
from boba.domain.core.workspace import (
    WorkspaceError,
    WorkspaceKind,
    WorkspaceNotFoundError,
    WorkspaceResolver,
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

    _BASE_NAME = "delete_file"
    _SOURCE = ToolSourceId("builtin.files")

    def __init__(
        self,
        resolver: WorkspaceResolver,
        workspace: WorkspaceKind,
    ) -> None:
        self._resolver = resolver
        self._workspace = workspace
        self._id = workspace_tool_id(workspace, self._BASE_NAME)

    def tool_id(self) -> ToolId:
        return self._id

    def tool_source_id(self) -> ToolSourceId:
        return self._SOURCE

    def typed_args_converter(self) -> Converter[dict[str, Any], DeleteFileArgs]:
        return DeleteFileArgsConverter()

    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            description=f"Удалить файл из workspace '{self._workspace.name}'.",
            input_schema=ToolInputSchema(
                params=[
                    ParamSchema(
                        name="filename",
                        description="Путь к файлу внутри workspace.",
                        validator=ChainValidator(Required(), IsString(), NonEmpty()),
                    ),
                ]
            ),
        )

    def execute(self, ctx: None, args: DeleteFileArgs) -> ToolResult:
        workspace = self._resolver.resolve(self._workspace)
        try:
            workspace.delete(args.filename)
        except WorkspaceNotFoundError as e:
            raise ToolExecutionError(
                tool_id=self._id, message=f"Файл не найден: {args.filename}"
            ) from e
        except WorkspaceError as e:
            raise ToolExecutionError(
                tool_id=self._id, message=f"Ошибка удаления: {e}"
            ) from e
        return ToolResult(
            content=f"Файл удалён: {self._workspace.name}:{args.filename}"
        )
