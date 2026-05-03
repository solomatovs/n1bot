"""Tool: метаданные файла или директории."""

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
    WorkspaceNotFoundError,
)


@dataclass(frozen=True)
class StatArgs:
    path: str


class StatTool(Tool[StatArgs]):
    """Метаданные файла или директории."""

    _ID = ToolId("stat")
    _SOURCE = ToolSourceId("builtin.files")

    def tool_id(self) -> ToolId:
        return self._ID

    def tool_source_id(self) -> ToolSourceId:
        return self._SOURCE

    def definition(self) -> ObjectSchema[StatArgs]:
        return ObjectSchema(
            description=(
                "Вернуть метаданные ресурса: тип (file/directory/other), "
                "размер в байтах, время модификации. Если ресурса нет — "
                "ошибка. Для директорий size — размер inode-блока ФС, не "
                "количество файлов; для содержимого директории — ls/tree."
            ),
            fields=[
                FieldSpec(
                    name="path",
                    description="Путь к файлу или директории.",
                    converter=ChainConverter(IsString(), NonEmpty()),
                    required=True,
                ),
            ],
            factory=StatArgs,
        )

    def execute(self, ctx: ToolContext, req: StatArgs) -> ToolResult:
        try:
            meta = ctx.project_workspace.meta(req.path)
        except WorkspaceNotFoundError as e:
            raise ToolExecutionError(
                tool_id=self._ID,
                message=f"Не найдено: {req.path}",
            ) from e
        except WorkspaceError as e:
            raise ToolExecutionError(
                tool_id=self._ID,
                message=f"Ошибка stat: {e}",
            ) from e

        body = (
            f"path: {meta.path}\n"
            f"kind: {meta.kind}\n"
            f"size: {meta.size}\n"
            f"modified: {meta.modified.isoformat()}"
        )
        return ToolResult(content=body)
