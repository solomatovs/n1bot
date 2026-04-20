"""Tool: удаление файла из workspace."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from boba.adapters.tools._workspace_arg import (
    WORKSPACE_PARAM_NAME,
    parse_workspace_arg,
    workspace_param_schema,
)
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
    USER_WORKSPACE_KIND,
    AllowedWorkspacesSpec,
    WorkspaceError,
    WorkspaceKind,
    WorkspaceNotFoundError,
    WorkspaceResolver,
)


@dataclass(frozen=True)
class DeleteFileArgs:
    filename: str
    workspace: WorkspaceKind = USER_WORKSPACE_KIND


class DeleteFileArgsConverter(Converter[dict[str, Any], DeleteFileArgs]):
    def __init__(self, allowed: AllowedWorkspacesSpec, tool_id: ToolId) -> None:
        self._allowed = allowed
        self._tool_id = tool_id

    def convert(self, value: dict[str, Any]) -> DeleteFileArgs:
        return DeleteFileArgs(
            filename=str(value["filename"]),
            workspace=parse_workspace_arg(
                value.get(WORKSPACE_PARAM_NAME), self._allowed, self._tool_id
            ),
        )


class DeleteFileTool(Tool[DeleteFileArgs]):
    """Удаление файла из workspace."""

    _ID = ToolId("delete_file")
    _SOURCE = ToolSourceId("builtin.files")

    def __init__(
        self,
        resolver: WorkspaceResolver,
        allowed: AllowedWorkspacesSpec,
    ) -> None:
        self._resolver = resolver
        self._allowed = allowed

    def tool_id(self) -> ToolId:
        return self._ID

    def tool_source_id(self) -> ToolSourceId:
        return self._SOURCE

    def args_converter(self) -> Converter[dict[str, Any], DeleteFileArgs]:
        return DeleteFileArgsConverter(self._allowed, self._ID)

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
                    workspace_param_schema(self._allowed),
                ]
            ),
        )

    def execute(self, ctx: None, args: DeleteFileArgs) -> ToolResult:
        workspace = self._resolver.resolve(args.workspace)
        try:
            workspace.delete(args.filename)
        except WorkspaceNotFoundError as e:
            raise ToolExecutionError(
                tool_id=self._ID, message=f"Файл не найден: {args.filename}"
            ) from e
        except WorkspaceError as e:
            raise ToolExecutionError(
                tool_id=self._ID, message=f"Ошибка удаления: {e}"
            ) from e
        return ToolResult(
            content=f"Файл удалён: {args.workspace.name}:{args.filename}"
        )
