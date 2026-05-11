"""Tool: показать текущую директорию."""

from __future__ import annotations

from dataclasses import dataclass

from boba.plugin.prompt import PromptOverlay
from boba.tools.domain import TextResult, Tool, ToolContext, ToolResult

__all__ = ["PwdArgs", "PwdTool", "PwdToolConfig"]


@dataclass(frozen=True)
class PwdArgs:
    """Вернуть путь текущей директории."""


@dataclass(frozen=True)
class PwdToolConfig:
    prompt: PromptOverlay


class PwdTool(Tool[PwdArgs, PwdToolConfig]):
    """Возвращает путь текущей директории."""

    def execute(self, ctx: ToolContext, req: PwdArgs) -> ToolResult:
        del req
        return TextResult(text=ctx.project_workspace.cwd)
