"""Файловые реализации WorkspaceShell / WorkspaceRegistry.

Состав пакета:

- _shell — FsWorkspaceShell (общая реализация) +
  concrete shells для Project/History/Scratch/Prompt namespace'ов.
- _registry — FsWorkspaceRegistry + concrete
  registries.
- _growbuffer — приватный buffered-I/O helper для
  read_text / streaming reads.

Пакет — отдельный pip-package; основной boba от него НЕ зависит.
Внешние потребители импортируют::

    from boba.adapter.fs_workspace import (
        FsProjectWorkspaceRegistry,
        FsHistoryWorkspaceRegistry,
        ...
    )
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
