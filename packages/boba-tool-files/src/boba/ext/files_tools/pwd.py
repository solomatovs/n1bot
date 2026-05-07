"""Tool: показать текущую директорию."""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from boba.declaration import ObjectSchema
from boba.plugin import ExtensionContext
from boba.plugin.prompt import PromptOverlay
from boba.tools.domain import (
    TextResult,
    Tool,
    ToolContext,
    ToolId,
    ToolResult,
    ToolSourceId,
)

__all__ = ["PwdTool", "PwdToolConfig"]


@dataclass(frozen=True)
class PwdArgs:
    """Пустой набор аргументов — pwd ничего не принимает."""


@dataclass(frozen=True)
class PwdToolConfig:
    prompt: PromptOverlay


class PwdTool(Tool[PwdArgs]):
    """Возвращает путь текущей директории."""

    _ID: ClassVar[ToolId] = ToolId("pwd")
    _SOURCE: ClassVar[ToolSourceId] = ToolSourceId("plugin.files")

    def __init__(self, cfg: PwdToolConfig, ctx: ExtensionContext) -> None:
        self._cfg = cfg
        self._ctx = ctx

    def tool_id(self) -> ToolId:
        return self._ID

    def tool_source_id(self) -> ToolSourceId:
        return self._SOURCE

    def definition(self) -> ObjectSchema[PwdArgs]:
        return self._cfg.prompt.apply(ObjectSchema(
            description="Вернуть путь текущей директории.",
            fields=[],
            factory=PwdArgs,
        ))

    def execute(self, ctx: ToolContext, req: PwdArgs) -> ToolResult:
        del req
        return TextResult(text=ctx.project_workspace.cwd)
