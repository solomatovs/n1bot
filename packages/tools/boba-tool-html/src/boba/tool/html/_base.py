"""Базовый класс html-tools: пуллит `ProjectWorkspaceShell` из ExtensionContext."""

from __future__ import annotations

from typing import Generic, TypeVar

from pydantic import BaseModel

from boba.tools.domain import Tool
from boba.workspace.contract import ProjectWorkspaceShell

__all__ = ["HtmlToolBase"]

TArgs = TypeVar("TArgs", bound=BaseModel)
TConfig = TypeVar("TConfig")


class HtmlToolBase(Tool[TArgs, TConfig], Generic[TArgs, TConfig]):
    """Tool с прямой ссылкой на project-workspace из build-time `ExtensionContext`."""

    def __init__(self, cfg: TConfig, ctx, source_id) -> None:
        super().__init__(cfg, ctx, source_id)
        self._shell: ProjectWorkspaceShell = ctx.get(ProjectWorkspaceShell)
