"""Tool: показать текущую директорию."""

from __future__ import annotations

from dataclasses import dataclass

from boba.plugin.prompt import PromptOverlay
from boba.tool.files._base import FsToolBase
from boba.tools.domain import TextResult, ToolContext, ToolResult

__all__ = ["PwdArgs", "PwdTool", "PwdToolConfig"]


@dataclass(frozen=True)
class PwdArgs:
    """Вернуть путь текущей директории."""


@dataclass(frozen=True)
class PwdToolConfig:
    prompt: PromptOverlay


class PwdTool(FsToolBase[PwdArgs, PwdToolConfig]):
    """Возвращает путь текущей директории."""

    def execute(self, ctx: ToolContext, req: PwdArgs) -> ToolResult:
        del req
        return TextResult(text=self._shell(ctx).cwd)
