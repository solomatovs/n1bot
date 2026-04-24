"""Tool: сменить текущую директорию."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from boba.domain.core.patterns import Converter
from boba.domain.core.tools import (
    ChainValidator,
    IsString,
    NonEmpty,
    ParamSchema,
    Pass,
    Required,
    Tool,
    ToolDefinition,
    ToolExecutionError,
    ToolId,
    ToolInputSchema,
    ToolResult,
    ToolSourceId,
)
from boba.domain.core.workspace import (
    ProjectWorkspaceShell,
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

    def __init__(self, workspace: ProjectWorkspaceShell) -> None:
        self._workspace = workspace

    def tool_id(self) -> ToolId:
        return self._ID

    def tool_source_id(self) -> ToolSourceId:
        return self._SOURCE

    def typed_args_converter(self) -> Converter[dict[str, Any], CdArgs]:
        return CdArgsConverter()

    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            description=(
                "Перейти в указанную директорию. Последующие относительные "
                "пути в других tool'ах будут разрешаться от неё. Если пути "
                "не существует или это не директория — возвращает ошибку."
            ),
            input_schema=ToolInputSchema(
                params=[
                    ParamSchema(
                        name="path",
                        description="Путь директории.",
                        validator=ChainValidator(Required(), IsString(), NonEmpty()),
                    ),
                ],
                invariants=Pass(),
            ),
        )

    def execute(self, ctx: None, args: CdArgs) -> ToolResult:
        try:
            self._workspace.cd(args.path)
        except WorkspaceNotFoundError as e:
            raise ToolExecutionError(
                tool_id=self._ID,
                message=f"Директория не найдена: {args.path}",
            ) from e
        except WorkspaceError as e:
            raise ToolExecutionError(
                tool_id=self._ID,
                message=f"Ошибка cd: {e}",
            ) from e
        return ToolResult(content=f"Текущая директория: {self._workspace.cwd}")
