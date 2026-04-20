"""Tool: запись/обновление содержимого файла в workspace."""

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
    WorkspaceResolver,
)


@dataclass(frozen=True)
class EditFileArgs:
    filename: str
    content: str
    workspace: WorkspaceKind = USER_WORKSPACE_KIND


class EditFileArgsConverter(Converter[dict[str, Any], EditFileArgs]):
    def __init__(self, allowed: AllowedWorkspacesSpec, tool_id: ToolId) -> None:
        self._allowed = allowed
        self._tool_id = tool_id

    def convert(self, value: dict[str, Any]) -> EditFileArgs:
        return EditFileArgs(
            filename=str(value["filename"]),
            content=str(value["content"]),
            workspace=parse_workspace_arg(
                value.get(WORKSPACE_PARAM_NAME), self._allowed, self._tool_id
            ),
        )


class EditFileTool(Tool[EditFileArgs]):
    """Запись/обновление файла в workspace (перезапись целиком)."""

    _ID = ToolId("edit_file")
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

    def args_converter(self) -> Converter[dict[str, Any], EditFileArgs]:
        return EditFileArgsConverter(self._allowed, self._ID)

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
                    workspace_param_schema(self._allowed),
                ]
            ),
        )

    def execute(self, ctx: None, args: EditFileArgs) -> ToolResult:
        workspace = self._resolver.resolve(args.workspace)
        existed = workspace.exists(args.filename)
        try:
            with workspace.write_text(args.filename) as f:
                f.write(args.content)
        except WorkspaceError as e:
            raise ToolExecutionError(
                tool_id=self._ID, message=f"Ошибка записи: {e}"
            ) from e

        action = "обновлён" if existed else "создан"
        return ToolResult(
            content=(
                f"Файл {action}: {args.workspace.name}:{args.filename} "
                f"({len(args.content)} символов)"
            )
        )
