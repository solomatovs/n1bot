"""Tool: сменить текущую директорию."""

from __future__ import annotations

from dataclasses import dataclass

from boba.declaration import FieldSpec, ObjectSchema
from boba.tools import (
    Tool,
    ToolContext,
    ToolExecutionError,
    ToolId,
    ToolResult,
    ToolSourceId,
)
from boba.validators import ChainConverter, IsString, NonEmpty
from boba.workspace import (
    WorkspaceError,
    WorkspaceNotFoundError,
)


@dataclass(frozen=True)
class CdArgs:
    path: str


class CdTool(Tool[CdArgs]):
    """Сменить текущую директорию."""

    _ID = ToolId("cd")
    _SOURCE = ToolSourceId("builtin.files")

    def tool_id(self) -> ToolId:
        return self._ID

    def tool_source_id(self) -> ToolSourceId:
        return self._SOURCE

    def definition(self) -> ObjectSchema[CdArgs]:
        return ObjectSchema(
            description="Сменить текущую директорию.",
            fields=[
                FieldSpec(
                    name="path",
                    description="Путь директории.",
                    converter=ChainConverter(IsString(), NonEmpty()),
                    required=True,
                ),
            ],
            factory=CdArgs,
        )

    def execute(self, ctx: ToolContext, req: CdArgs) -> ToolResult:
        try:
            ctx.project_workspace.cd(req.path)
        except WorkspaceNotFoundError as e:
            raise ToolExecutionError(
                tool_id=self._ID,
                message=f"Директория не найдена: {req.path}",
            ) from e
        except WorkspaceError as e:
            raise ToolExecutionError(
                tool_id=self._ID,
                message=f"Ошибка cd: {e}",
            ) from e
        return ToolResult(content=f"Текущая директория: {ctx.project_workspace.cwd}")
