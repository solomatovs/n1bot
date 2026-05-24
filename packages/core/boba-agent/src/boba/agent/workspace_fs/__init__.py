"""Файловые реализации WorkspaceShell / WorkspaceRegistry."""

from boba.agent.workspace_fs.registry import (
    FsHistoryWorkspaceRegistry,
    FsProjectWorkspaceRegistry,
    FsPromptWorkspaceRegistry,
    FsScratchWorkspaceRegistry,
    FsWorkspaceRegistry,
)
from boba.agent.workspace_fs.shell import (
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
]
