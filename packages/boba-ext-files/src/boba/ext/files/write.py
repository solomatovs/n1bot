"""Tool: перезаписать файл целиком."""

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
from boba_next.validators import ChainConverter, Default, IsString, NonEmpty
from boba_next.workspace import (
    WorkspaceError,
)


@dataclass(frozen=True)
class WriteArgs:
    path: str
    content: str
    encoding: str


class WriteTool(Tool[WriteArgs]):
    """Полностью перезаписать файл содержимым."""

    _ID = ToolId("write")
    _SOURCE = ToolSourceId("builtin.files")

    def tool_id(self) -> ToolId:
        return self._ID

    def tool_source_id(self) -> ToolSourceId:
        return self._SOURCE

    def definition(self) -> ObjectSchema[WriteArgs]:
        return ObjectSchema(
            description=(
                "Перезаписать файл указанным содержимым. Если файла или "
                "промежуточных директорий нет — создать."
            ),
            fields=[
                FieldSpec(
                    name="path",
                    description="Путь к файлу.",
                    converter=ChainConverter(IsString(), NonEmpty()),
                    required=True,
                ),
                FieldSpec(
                    name="content",
                    description="Новое содержимое файла.",
                    converter=ChainConverter(IsString()),
                    required=True,
                ),
                FieldSpec(
                    name="encoding",
                    description="Кодировка файла. По умолчанию 'utf-8'.",
                    converter=ChainConverter(
                        Default("utf-8"),
                        IsString(),
                        NonEmpty(),
                    ),
                ),
            ],
            factory=WriteArgs,
        )

    def execute(self, ctx: ToolContext, req: WriteArgs) -> ToolResult:
        existed = ctx.project_workspace.exists(req.path)
        try:
            with ctx.project_workspace.write_text(req.path, req.encoding) as f:
                f.write(req.content)
        except WorkspaceError as e:
            raise ToolExecutionError(
                tool_id=self._ID,
                message=f"Ошибка записи: {e}",
            ) from e
        action = "обновлён" if existed else "создан"
        return ToolResult(
            content=f"Файл {action}: {req.path} ({len(req.content)} символов)",
        )
