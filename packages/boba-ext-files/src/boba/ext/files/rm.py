"""Tool: удаление файла или директории (rm / rm -r)."""

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
from boba_next.validators import (
    ChainConverter,
    Default,
    IsBool,
    IsString,
    NonEmpty,
)
from boba_next.workspace import (
    WorkspaceError,
    WorkspaceNotFoundError,
)


@dataclass(frozen=True)
class RmArgs:
    path: str
    recursive: bool


class RmTool(Tool[RmArgs]):
    """Удалить файл или директорию."""

    _ID = ToolId("rm")
    _SOURCE = ToolSourceId("builtin.files")

    def tool_id(self) -> ToolId:
        return self._ID

    def tool_source_id(self) -> ToolSourceId:
        return self._SOURCE

    def definition(self) -> ObjectSchema[RmArgs]:
        return ObjectSchema(
            description=(
                "Удалить файл или директорию. Для директорий требуется "
                "recursive=true. Безвозвратно."
            ),
            fields=[
                FieldSpec(
                    name="path",
                    description="Путь к файлу или директории.",
                    converter=ChainConverter(IsString(), NonEmpty()),
                    required=True,
                ),
                FieldSpec(
                    name="recursive",
                    description=(
                        "Удалить директорию со всем содержимым. По умолчанию false."
                    ),
                    converter=ChainConverter(Default(False), IsBool()),
                ),
            ],
            factory=RmArgs,
        )

    def execute(self, ctx: ToolContext, req: RmArgs) -> ToolResult:
        try:
            ctx.project_workspace.delete(req.path, recursive=req.recursive)
        except WorkspaceNotFoundError as e:
            raise ToolExecutionError(
                tool_id=self._ID,
                message=f"Не найдено: {req.path}",
            ) from e
        except WorkspaceError as e:
            raise ToolExecutionError(
                tool_id=self._ID,
                message=f"Ошибка удаления: {e}",
            ) from e
        return ToolResult(content=f"Удалено: {req.path}")
