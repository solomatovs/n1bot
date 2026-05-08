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
    ToolName,
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

    _NAME: ClassVar[ToolName] = ToolName("pwd")

    def __init__(self, cfg: PwdToolConfig, ctx: ExtensionContext, source_id: ToolSourceId) -> None:
        self._cfg = cfg
        self._ctx = ctx
        self._tool_id = ToolId.compose(source_id, self._NAME)

    def tool_id(self) -> ToolId:
        return self._tool_id


    def definition(self) -> ObjectSchema[PwdArgs]:
        return self._cfg.prompt.apply(ObjectSchema(
            description="Вернуть путь текущей директории.",
            fields=[],
            factory=PwdArgs,
        ))

    def execute(self, ctx: ToolContext, req: PwdArgs) -> ToolResult:
        del req
        return TextResult(text=ctx.project_workspace.cwd)
