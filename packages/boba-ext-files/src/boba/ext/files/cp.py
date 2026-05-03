"""Tool: копирование файла или директории (cp / cp -r)."""

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
from boba.validators import (
    ChainConverter,
    Default,
    IsBool,
    IsString,
    NonEmpty,
)
from boba.workspace import (
    WorkspaceError,
    WorkspaceNotFoundError,
)


@dataclass(frozen=True)
class CpArgs:
    src: str
    dst: str
    recursive: bool


class CpTool(Tool[CpArgs]):
    """Скопировать файл или директорию."""

    _ID = ToolId("cp")
    _SOURCE = ToolSourceId("builtin.files")

    def tool_id(self) -> ToolId:
        return self._ID

    def tool_source_id(self) -> ToolSourceId:
        return self._SOURCE

    def definition(self) -> ObjectSchema[CpArgs]:
        return ObjectSchema(
            description=(
                "Скопировать файл или директорию. Для директорий "
                "требуется recursive=true."
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
                FieldSpec(
                    name="recursive",
                    description=(
                        "Рекурсивное копирование директории. По умолчанию false."
                    ),
                    converter=ChainConverter(Default(False), IsBool()),
                ),
            ],
            factory=CpArgs,
        )

    def execute(self, ctx: ToolContext, req: CpArgs) -> ToolResult:
        try:
            ctx.project_workspace.copy(req.src, req.dst, recursive=req.recursive)
        except WorkspaceNotFoundError as e:
            raise ToolExecutionError(
                tool_id=self._ID,
                message=f"Источник не найден: {req.src}",
            ) from e
        except WorkspaceError as e:
            raise ToolExecutionError(
                tool_id=self._ID,
                message=f"Ошибка копирования: {e}",
            ) from e
        return ToolResult(content=f"Скопировано: {req.src} → {req.dst}")
