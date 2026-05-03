"""Tool: показать текущую директорию."""

from __future__ import annotations

from dataclasses import dataclass

from boba_next.declaration import ObjectSchema
from boba_next.tools import Tool, ToolContext, ToolId, ToolResult, ToolSourceId


@dataclass(frozen=True)
class PwdArgs:
    """Пустой набор аргументов — pwd ничего не принимает."""


class PwdTool(Tool[PwdArgs]):
    """Возвращает путь текущей директории."""

    _ID = ToolId("pwd")
    _SOURCE = ToolSourceId("builtin.files")

    def tool_id(self) -> ToolId:
        return self._ID

    def tool_source_id(self) -> ToolSourceId:
        return self._SOURCE

    def definition(self) -> ObjectSchema[PwdArgs]:
        return ObjectSchema(
            fields=[],
            factory=PwdArgs,
            description="Вернуть путь текущей директории.",
        )

    def execute(self, ctx: ToolContext, req: PwdArgs) -> ToolResult:
        return ToolResult(content=ctx.project_workspace.cwd)
