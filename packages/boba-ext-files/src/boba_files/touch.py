"""Tool: создать пустой файл или обновить mtime."""

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
class TouchArgs:
    path: str


class TouchArgsConverter(Converter[dict[str, Any], TouchArgs]):
    def convert(self, value: dict[str, Any]) -> TouchArgs:
        return TouchArgs(path=value["path"])


class TouchTool(Tool[TouchArgs]):
    """Создать пустой файл или обновить mtime существующего."""

    _ID = ToolId("touch")
    _SOURCE = ToolSourceId("builtin.files")

    def tool_id(self) -> ToolId:
        return self._ID

    def tool_source_id(self) -> ToolSourceId:
        return self._SOURCE

    def typed_args_converter(self) -> Converter[dict[str, Any], TouchArgs]:
        return TouchArgsConverter()

    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            description=(
                "Создать пустой файл (включая промежуточные директории). "
                "Если уже существует — обновить время модификации, "
                "содержимое не трогать."
            ),
            input_schema=ToolInputSchema(
                params=[
                    ParamSchema(
                        name="path",
                        description="Путь к файлу.",
                        validator=ChainValidator(Required(), IsString(), NonEmpty()),
                    ),
                ],
                invariants=Pass(),
            ),
        )

    def execute(self, ctx: ToolContext, req: TouchArgs) -> ToolResult:
        try:
            ctx.project_workspace.touch(req.path)
        except WorkspaceError as e:
            raise ToolExecutionError(
                tool_id=self._ID,
                message=f"Ошибка touch: {e}",
            ) from e
        return ToolResult(content=f"touch: {req.path}")

