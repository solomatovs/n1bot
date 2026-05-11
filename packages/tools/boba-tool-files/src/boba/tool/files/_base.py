"""
Базовый класс file-tools: пуллит `ProjectWorkspaceRegistry` из ExtensionContext
"""

from __future__ import annotations

from typing import Generic, TypeVar

from boba.tools.domain import Tool, ToolContext
from boba.workspace.contract import (
    ProjectWorkspaceRegistry,
    WorkspaceId,
    WorkspaceShell,
)

__all__ = ["FsToolBase"]

TArgs = TypeVar("TArgs")
TConfig = TypeVar("TConfig")


class FsToolBase(Tool[TArgs, TConfig], Generic[TArgs, TConfig]):
    """
    Tool с доступом к project-workspace через build-time registry.

    Конструктор забирает `ProjectWorkspaceRegistry`
    из реестра shared служб - `ExtensionContext`
    """

    def __init__(self, cfg: TConfig, ctx, source_id) -> None:
        super().__init__(cfg, ctx, source_id)
        self._workspaces: ProjectWorkspaceRegistry = ctx.get(ProjectWorkspaceRegistry)

    def _shell(self, ctx: ToolContext) -> WorkspaceShell[WorkspaceId]:
        return self._workspaces.get_or_create(ctx.workspace_id)
