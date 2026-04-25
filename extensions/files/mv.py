"""Tool: переместить/переименовать файл или директорию."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from boba.adapters.tool_providers import StaticToolSource
from boba.domain.core.patterns import Converter
from boba.domain.core.tools import (
    ChainValidator,
    IsString,
    NonEmpty,
    ParamSchema,
    Pass,
    Required,
    Tool,
    ToolContext,
    ToolDefinition,
    ToolExecutionError,
    ToolId,
    ToolInputSchema,
    ToolResult,
    ToolSource,
    ToolSourceId,
)
from boba.domain.core.workspace import (
    WorkspaceError,
    WorkspaceNotFoundError,
)
from boba.infra.extensions import ExtensionContext


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

    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            description=(
                "Переместить или переименовать файл/директорию. Если dst — "
                "существующая директория, src переносится внутрь. Файл по "
                "пути dst перезаписывается. Промежуточные директории не "
                "создаются."
            ),
            input_schema=ToolInputSchema(
                params=[
                    ParamSchema(
                        name="src",
                        description="Путь источника.",
                        validator=ChainValidator(Required(), IsString(), NonEmpty()),
                    ),
                    ParamSchema(
                        name="dst",
                        description="Путь назначения.",
                        validator=ChainValidator(Required(), IsString(), NonEmpty()),
                    ),
                ],
                invariants=Pass(),
            ),
        )

    def execute(self, ctx: ToolContext, args: MvArgs) -> ToolResult:
        try:
            ctx.project_workspace.move(args.src, args.dst)
        except WorkspaceNotFoundError as e:
            raise ToolExecutionError(
                tool_id=self._ID,
                message=f"Источник не найден: {args.src}",
            ) from e
        except WorkspaceError as e:
            raise ToolExecutionError(
                tool_id=self._ID,
                message=f"Ошибка перемещения: {e}",
            ) from e
        return ToolResult(content=f"Перемещено: {args.src} → {args.dst}")

def register_tools(ctx: ExtensionContext) -> Iterable[ToolSource]:
    yield StaticToolSource(
        ToolSourceId("builtin.files.mv"),
        priority=0,
        tools=[MvTool()],
    )
