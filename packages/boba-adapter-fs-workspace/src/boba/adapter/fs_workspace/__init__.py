"""Файловые реализации WorkspaceShell / WorkspaceRegistry.

Отдельный pip-пакет; core boba от него не зависит.
"""

from boba.adapter.fs_workspace.config import WorkspacesSection
from boba.adapter.fs_workspace.registry import (
    FsHistoryWorkspaceRegistry,
    FsProjectWorkspaceRegistry,
    FsPromptWorkspaceRegistry,
    FsScratchWorkspaceRegistry,
    FsWorkspaceRegistry,
)
from boba.adapter.fs_workspace.shell import (
    FsHistoryWorkspaceShell,
    FsProjectWorkspaceShell,
    FsPromptWorkspaceShell,
    FsScratchWorkspaceShell,
    FsWorkspaceShell,
    WorkspacePath,
)

__all__ = [
    "FsHistoryWorkspaceRegistry",
    "FsHistoryWorkspaceShell",
    "FsProjectWorkspaceRegistry",
    "FsProjectWorkspaceShell",
    "FsPromptWorkspaceRegistry",
    "FsPromptWorkspaceShell",
    "FsScratchWorkspaceRegistry",
    "FsScratchWorkspaceShell",
    "FsWorkspaceRegistry",
    "FsWorkspaceShell",
    "WorkspacePath",
    "WorkspacesSection",
]
