"""Tool: сменить текущую директорию."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from boba.domain.core.patterns import Converter
from boba.domain.core.tools import (
    ChainConverter,
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
    ToolSourceId,
)
from boba.domain.core.workspace import (
    WorkspaceError,
    WorkspaceNotFoundError,
)


@dataclass(frozen=True)
class CdArgs:
    path: str


class CdArgsConverter(Converter[dict[str, Any], CdArgs]):
    def convert(self, value: dict[str, Any]) -> CdArgs:
        return CdArgs(path=value["path"])


class CdTool(Tool[CdArgs]):
    """Сменить текущую директорию."""

    _ID = ToolId("cd")
    _SOURCE = ToolSourceId("builtin.files")

    def tool_id(self) -> ToolId:
        return self._ID

    def tool_source_id(self) -> ToolSourceId:
        return self._SOURCE

    def typed_args_converter(self) -> Converter[dict[str, Any], CdArgs]:
        return CdArgsConverter()

    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            description="Сменить текущую директорию.",
            input_schema=ToolInputSchema(
                params=[
                    ParamSchema(
                        name="path",
                        description="Путь директории.",
                        converter=ChainConverter(Required(), IsString(), NonEmpty()),
                    ),
                ],
                invariants=Pass(),
            ),
        )

    def execute(self, ctx: ToolContext, req: CdArgs) -> ToolResult:
        try:
            ctx.project_workspace.cd(req.path)
        except WorkspaceNotFoundError as e:
            raise ToolExecutionError(
                tool_id=self._ID,
                message=f"Директория не найдена: {req.path}",
            ) from e
        except WorkspaceError as e:
            raise ToolExecutionError(
                tool_id=self._ID,
                message=f"Ошибка cd: {e}",
            ) from e
        return ToolResult(content=f"Текущая директория: {ctx.project_workspace.cwd}")

