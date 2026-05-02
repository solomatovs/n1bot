"""Tool: переместить/переименовать файл или директорию."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from boba.domain.core.tools import (
    ChainConverter,
    FieldSpec,
    IsString,
    NonEmpty,
    ObjectSchema,
    Pass,
    Required,
    Tool,
    ToolContext,
    ToolExecutionError,
    ToolId,
    ToolResult,
    ToolSourceId,
)
from boba.domain.core.workspace import (
    WorkspaceError,
    WorkspaceNotFoundError,
)
from boba.patterns import Converter


@dataclass(frozen=True)
class MvArgs:
    src: str
    dst: str


class MvArgsConverter(Converter[dict[str, Any], MvArgs]):
    def convert(self, value: dict[str, Any]) -> MvArgs:
        return MvArgs(src=value["src"], dst=value["dst"])


class MvTool(Tool[MvArgs]):
    """Переместить/переименовать файл или директорию."""

    _ID = ToolId("mv")
    _SOURCE = ToolSourceId("builtin.files")

    def tool_id(self) -> ToolId:
        return self._ID

    def tool_source_id(self) -> ToolSourceId:
        return self._SOURCE

    def typed_args_converter(self) -> Converter[dict[str, Any], MvArgs]:
        return MvArgsConverter()

    def definition(self) -> ObjectSchema[dict[str, Any]]:
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
                        converter=ChainConverter(Required(), IsString(), NonEmpty()),
                    ),
                    FieldSpec(
                        name="dst",
                        description="Путь назначения.",
                        converter=ChainConverter(Required(), IsString(), NonEmpty()),
                    ),
                ],
                invariants=Pass()
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

