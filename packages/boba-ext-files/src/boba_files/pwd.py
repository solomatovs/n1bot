"""Tool: показать текущую директорию."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from boba.domain.core.patterns import Converter
from boba.domain.core.tools import (
    Pass,
    Tool,
    ToolContext,
    ToolDefinition,
    ToolId,
    ObjectSchema,
    ToolResult,
    ToolSourceId,
)


@dataclass(frozen=True)
class PwdArgs:
    """Пустой набор аргументов — pwd ничего не принимает."""


class PwdArgsConverter(Converter[dict[str, Any], PwdArgs]):
    def convert(self, value: dict[str, Any]) -> PwdArgs:
        return PwdArgs()


class PwdTool(Tool[PwdArgs]):
    """Возвращает путь текущей директории."""

    _ID = ToolId("pwd")
    _SOURCE = ToolSourceId("builtin.files")

    def tool_id(self) -> ToolId:
        return self._ID

    def tool_source_id(self) -> ToolSourceId:
        return self._SOURCE

    def typed_args_converter(self) -> Converter[dict[str, Any], PwdArgs]:
        return PwdArgsConverter()

    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            description="Вернуть путь текущей директории.",
            input_schema=ObjectSchema(fields=[], invariants=Pass()),
        )

    def execute(self, ctx: ToolContext, req: PwdArgs) -> ToolResult:
        return ToolResult(content=ctx.project_workspace.cwd)

