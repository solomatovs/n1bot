"""Базовый класс file-tools: пуллит `ProjectWorkspaceShell` из ExtensionContext."""

from __future__ import annotations

from typing import Generic, TypeVar

from boba.tools.domain import Tool
from boba.workspace.contract import ProjectWorkspaceShell

__all__ = ["FsToolBase"]

TArgs = TypeVar("TArgs")
TConfig = TypeVar("TConfig")


class FsToolBase(Tool[TArgs, TConfig], Generic[TArgs, TConfig]):
    """Tool с прямой ссылкой на project-workspace из build-time `ExtensionContext`."""

    def __init__(self, cfg: TConfig, ctx, source_id) -> None:
        super().__init__(cfg, ctx, source_id)
        self._shell: ProjectWorkspaceShell = ctx.get(ProjectWorkspaceShell)
