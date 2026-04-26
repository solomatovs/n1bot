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

    def tool_id(self) -> ToolId:
        return self._ID

    def tool_source_id(self) -> ToolSourceId:
        return self._SOURCE

    def typed_args_converter(self) -> Converter[dict[str, Any], RmArgs]:
        return RmArgsConverter()

    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            description=(
                "Удалить файл или директорию. Для директорий требуется "
                "recursive=true. Безвозвратно."
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
                            "Удалить директорию со всем содержимым. "
                            "По умолчанию false."
                        ),
                        validator=ChainValidator(Default(False), IsBool()),
                    ),
                ],
                invariants=Pass(),
            ),
        )

    def execute(self, ctx: ToolContext, req: RmArgs) -> ToolResult:
        try:
            ctx.project_workspace.delete(req.path, recursive=req.recursive)
        except WorkspaceNotFoundError as e:
            raise ToolExecutionError(
                tool_id=self._ID,
                message=f"Не найдено: {req.path}",
            ) from e
        except WorkspaceError as e:
            raise ToolExecutionError(
                tool_id=self._ID,
                message=f"Ошибка удаления: {e}",
            ) from e
        return ToolResult(content=f"Удалено: {req.path}")

