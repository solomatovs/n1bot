"""Tool: создать директорию."""

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
    UserWorkspaceService,
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

    def __init__(self, workspace: UserWorkspaceService) -> None:
        self._workspace = workspace

    def tool_id(self) -> ToolId:
        return self._ID

    def tool_source_id(self) -> ToolSourceId:
        return self._SOURCE

    def typed_args_converter(self) -> Converter[dict[str, Any], MkdirArgs]:
        return MkdirArgsConverter()

    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            description=(
                "Создать директорию. Промежуточные директории создаются "
                "автоматически. Если директория уже существует — ничего не "
                "делает. Если по этому пути лежит файл — возвращает ошибку."
            ),
            input_schema=ToolInputSchema(
                params=[
                    ParamSchema(
                        name="path",
                        description="Путь создаваемой директории.",
                        validator=ChainValidator(Required(), IsString(), NonEmpty()),
                    ),
                ],
                invariants=Pass(),
            ),
        )

    def execute(self, ctx: None, args: MkdirArgs) -> ToolResult:
        try:
            self._workspace.mkdir(args.path)
        except WorkspaceError as e:
            raise ToolExecutionError(
                tool_id=self._ID, message=f"Ошибка mkdir: {e}",
            ) from e
        return ToolResult(content=f"Директория создана: {args.path}")
