"""Tool: удаление файла или директории (rm / rm -r)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from boba.domain.core.patterns import Converter
from boba.domain.core.tools import (
    ChainValidator,
    Default,
    IsBool,
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
class RmArgs:
    path: str
    recursive: bool


class RmArgsConverter(Converter[dict[str, Any], RmArgs]):
    def convert(self, value: dict[str, Any]) -> RmArgs:
        return RmArgs(path=value["path"], recursive=value["recursive"])


class RmTool(Tool[RmArgs]):
    """Удалить файл или директорию."""

    _ID = ToolId("rm")
    _SOURCE = ToolSourceId("builtin.files")

    def __init__(self, workspace: ProjectWorkspaceShell) -> None:
        self._workspace = workspace

    def tool_id(self) -> ToolId:
        return self._ID

    def tool_source_id(self) -> ToolSourceId:
        return self._SOURCE

    def typed_args_converter(self) -> Converter[dict[str, Any], RmArgs]:
        return RmArgsConverter()

    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            description=(
                "Удалить файл или директорию. Файл удаляется всегда. "
                "Директория удаляется только с recursive=true (со всем "
                "содержимым); без флага на директории возвращает ошибку. "
                "Операция безвозвратна."
            ),
            input_schema=ToolInputSchema(
                params=[
                    ParamSchema(
                        name="path",
                        description="Путь к файлу или директории.",
                        validator=ChainValidator(Required(), IsString(), NonEmpty()),
                    ),
                    ParamSchema(
                        name="recursive",
                        description=(
                            "Если true — рекурсивно удалить директорию со всем "
                            "содержимым. По умолчанию false."
                        ),
                        validator=ChainValidator(Default(False), IsBool()),
                    ),
                ],
                invariants=Pass(),
            ),
        )

    def execute(self, ctx: None, args: RmArgs) -> ToolResult:
        try:
            self._workspace.delete(args.path, recursive=args.recursive)
        except WorkspaceNotFoundError as e:
            raise ToolExecutionError(
                tool_id=self._ID,
                message=f"Не найдено: {args.path}",
            ) from e
        except WorkspaceError as e:
            raise ToolExecutionError(
                tool_id=self._ID,
                message=f"Ошибка удаления: {e}",
            ) from e
        return ToolResult(content=f"Удалено: {args.path}")
