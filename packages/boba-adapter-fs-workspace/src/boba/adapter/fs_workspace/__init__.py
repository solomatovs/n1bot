"""Файловые реализации :class:`WorkspaceShell` / :class:`WorkspaceRegistry`.

Состав пакета:

- :mod:`._shell` — :class:`FsWorkspaceShell` (общая реализация) +
  concrete shells для Project/History/Scratch/Prompt namespace'ов.
- :mod:`._registry` — :class:`FsWorkspaceRegistry` + concrete
  registries.
- :mod:`._growbuffer` — приватный buffered-I/O helper для
  :meth:`FsWorkspaceShell.read_text` / streaming reads.

Пакет — отдельный pip-package; основной ``boba`` от него НЕ зависит.
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
