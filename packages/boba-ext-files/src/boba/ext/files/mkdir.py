"""Tool: создать директорию."""

from __future__ import annotations

from dataclasses import dataclass

from boba_next.declaration import FieldSpec, ObjectSchema
from boba_next.tools import (
    Tool,
    ToolContext,
    ToolExecutionError,
    ToolId,
    ToolResult,
    ToolSourceId,
)
from boba_next.validators import ChainConverter, IsString, NonEmpty
from boba_next.workspace import (
    WorkspaceError,
)


@dataclass(frozen=True)
class MkdirArgs:
    path: str


class MkdirTool(Tool[MkdirArgs]):
    """Создать директорию."""

    _ID = ToolId("mkdir")
    _SOURCE = ToolSourceId("builtin.files")

    def tool_id(self) -> ToolId:
        return self._ID

    def tool_source_id(self) -> ToolSourceId:
        return self._SOURCE

    def definition(self) -> ObjectSchema[MkdirArgs]:
        return ObjectSchema(
            description=(
                "Создать директорию (включая промежуточные). Если уже "
                "существует — no-op. Если по пути файл — ошибка."
            ),
            fields=[
                FieldSpec(
                    name="path",
                    description="Путь создаваемой директории.",
                    converter=ChainConverter(IsString(), NonEmpty()),
                    required=True,
                ),
            ],
            factory=MkdirArgs,
        )

    def execute(self, ctx: ToolContext, req: MkdirArgs) -> ToolResult:
        try:
            ctx.project_workspace.mkdir(req.path)
        except WorkspaceError as e:
            raise ToolExecutionError(
                tool_id=self._ID,
                message=f"Ошибка mkdir: {e}",
            ) from e
        return ToolResult(content=f"Директория создана: {req.path}")
