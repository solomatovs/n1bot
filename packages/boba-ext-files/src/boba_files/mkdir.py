"""Tool: создать директорию."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from boba.domain.core.patterns import Converter
from boba.domain.core.tools import (
    ChainConverter,
    IsString,
    NonEmpty,
    FieldSpec,
    Pass,
    Required,
    Tool,
    ToolContext,
    ToolDefinition,
    ToolExecutionError,
    ToolId,
    ToolInputSchema,
    ToolResult,
    ToolSourceId,
)
from boba.domain.core.workspace import (
    WorkspaceError,
)


@dataclass(frozen=True)
class MkdirArgs:
    path: str


class MkdirArgsConverter(Converter[dict[str, Any], MkdirArgs]):
    def convert(self, value: dict[str, Any]) -> MkdirArgs:
        return MkdirArgs(path=value["path"])


class MkdirTool(Tool[MkdirArgs]):
    """Создать директорию."""

    _ID = ToolId("mkdir")
    _SOURCE = ToolSourceId("builtin.files")

    def tool_id(self) -> ToolId:
        return self._ID

    def tool_source_id(self) -> ToolSourceId:
        return self._SOURCE

    def typed_args_converter(self) -> Converter[dict[str, Any], MkdirArgs]:
        return MkdirArgsConverter()

    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            description=(
                "Создать директорию (включая промежуточные). Если уже "
                "существует — no-op. Если по пути файл — ошибка."
            ),
            input_schema=ToolInputSchema(
                params=[
                    FieldSpec(
                        name="path",
                        description="Путь создаваемой директории.",
                        converter=ChainConverter(Required(), IsString(), NonEmpty()),
                    ),
                ],
                invariants=Pass(),
            ),
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

