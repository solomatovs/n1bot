"""Tool: показать текущую директорию."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from boba.adapters.tool_providers import StaticToolSource
from boba.domain.core.patterns import Converter
from boba.domain.core.tools import (
    Pass,
    Tool,
    ToolDefinition,
    ToolId,
    ToolInputSchema,
    ToolResult,
    ToolSource,
    ToolSourceId,
)
from boba.domain.core.workspace import ProjectWorkspaceShell
from boba.infra.plugins import PluginContext


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

    def __init__(self, workspace: ProjectWorkspaceShell) -> None:
        self._workspace = workspace

    def tool_id(self) -> ToolId:
        return self._ID

    def tool_source_id(self) -> ToolSourceId:
        return self._SOURCE

    def typed_args_converter(self) -> Converter[dict[str, Any], PwdArgs]:
        return PwdArgsConverter()

    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            description="Показать путь текущей директории.",
            input_schema=ToolInputSchema(params=[], invariants=Pass()),
        )

    def execute(self, ctx: None, args: PwdArgs) -> ToolResult:
        return ToolResult(content=self._workspace.cwd)

def register(ctx: PluginContext) -> Iterable[ToolSource]:
    yield StaticToolSource(
        ToolSourceId("builtin.files.pwd"),
        priority=0,
        tools=[PwdTool(ctx.project_workspace)],
    )
