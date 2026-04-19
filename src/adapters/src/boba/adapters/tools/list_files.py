"""Tool: список файлов в workspace (рекурсивно, без фильтрации)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from boba.domain.core.patterns import Converter
from boba.domain.core.tools import (
    Tool,
    ToolDefinition,
    ToolId,
    ToolInputSchema,
    ToolResult,
    ToolSourceId,
)
from boba.domain.core.workspace import WorkspaceError, WorkspaceService


@dataclass(frozen=True)
class ListFilesArgs:
    pass


class ListFilesArgsConverter(Converter[dict[str, Any], ListFilesArgs]):
    def convert(self, value: dict[str, Any]) -> ListFilesArgs:
        return ListFilesArgs()


class ListFilesTool(Tool[ListFilesArgs]):
    """Список всех файлов в workspace (рекурсивно)."""

    _ID = ToolId("list_files")
    _SOURCE = ToolSourceId("builtin.files")

    def __init__(self, workspace: WorkspaceService) -> None:
        self._workspace = workspace

    def tool_id(self) -> ToolId:
        return self._ID

    def tool_source_id(self) -> ToolSourceId:
        return self._SOURCE

    def args_converter(self) -> Converter[dict[str, Any], ListFilesArgs]:
        return ListFilesArgsConverter()

    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            description="Показать все файлы в workspace (рекурсивно, без фильтрации).",
            input_schema=ToolInputSchema(params=[]),
        )

    def execute(self, ctx: None, args: ListFilesArgs) -> ToolResult:
        try:
            items = sorted(self._workspace.tree())
        except WorkspaceError as e:
            return ToolResult(content=f"Ошибка обхода workspace: {e}", is_error=True)

        if not items:
            return ToolResult(content="Workspace пуст.")

        body = "\n".join(f"- {p}" for p in items)
        return ToolResult(content=f"Файлы ({len(items)}):\n{body}")
