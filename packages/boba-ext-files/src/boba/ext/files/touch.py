"""Tool: создать пустой файл или обновить mtime."""

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
)


@dataclass(frozen=True)
class TouchArgs:
    path: str


class TouchTool(Tool[TouchArgs]):
    """Создать пустой файл или обновить mtime существующего."""

    _ID = ToolId("touch")
    _SOURCE = ToolSourceId("builtin.files")

    def tool_id(self) -> ToolId:
        return self._ID

    def tool_source_id(self) -> ToolSourceId:
        return self._SOURCE

    def definition(self) -> ObjectSchema[TouchArgs]:
        return ObjectSchema(
            description=(
                "Создать пустой файл (включая промежуточные директории). "
                "Если уже существует — обновить время модификации, "
                "содержимое не трогать."
            ),
            fields=[
                FieldSpec(
                    name="path",
                    description="Путь к файлу.",
                    converter=ChainConverter(IsString(), NonEmpty()),
                    required=True,
                ),
            ],
            factory=TouchArgs,
        )

    def execute(self, ctx: ToolContext, req: TouchArgs) -> ToolResult:
        try:
            ctx.project_workspace.touch(req.path)
        except WorkspaceError as e:
            raise ToolExecutionError(
                tool_id=self._ID,
                message=f"Ошибка touch: {e}",
            ) from e
        return ToolResult(content=f"touch: {req.path}")
