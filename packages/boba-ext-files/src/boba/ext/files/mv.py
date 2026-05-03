"""Tool: переместить/переименовать файл или директорию."""

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
class MvArgs:
    src: str
    dst: str


class MvTool(Tool[MvArgs]):
    """Переместить/переименовать файл или директорию."""

    _ID = ToolId("mv")
    _SOURCE = ToolSourceId("builtin.files")

    def tool_id(self) -> ToolId:
        return self._ID

    def tool_source_id(self) -> ToolSourceId:
        return self._SOURCE

    def definition(self) -> ObjectSchema[MvArgs]:
        return ObjectSchema(
            description=(
                "Переместить или переименовать файл/директорию. Если dst — "
                "существующая директория, src переносится внутрь. Файл по "
                "пути dst перезаписывается. Промежуточные директории не "
                "создаются."
            ),
            fields=[
                FieldSpec(
                    name="src",
                    description="Путь источника.",
                    converter=ChainConverter(IsString(), NonEmpty()),
                    required=True,
                ),
                FieldSpec(
                    name="dst",
                    description="Путь назначения.",
                    converter=ChainConverter(IsString(), NonEmpty()),
                    required=True,
                ),
            ],
            factory=MvArgs,
        )

    def execute(self, ctx: ToolContext, req: MvArgs) -> ToolResult:
        try:
            ctx.project_workspace.move(req.src, req.dst)
        except WorkspaceNotFoundError as e:
            raise ToolExecutionError(
                tool_id=self._ID,
                message=f"Источник не найден: {req.src}",
            ) from e
        except WorkspaceError as e:
            raise ToolExecutionError(
                tool_id=self._ID,
                message=f"Ошибка перемещения: {e}",
            ) from e
        return ToolResult(content=f"Перемещено: {req.src} → {req.dst}")
