"""Файловая реализация WorkspaceService."""

from __future__ import annotations

from pathlib import Path

from boba.domain.core.workspace import WorkspaceId, WorkspaceService


class FsWorkspaceService(WorkspaceService):
    """Работает с файлами внутри директории workspace'а."""

    def __init__(self, workspace_id: WorkspaceId, root: Path) -> None:
        self._workspace_id = workspace_id
        self._root = root

    @property
    def workspace_id(self) -> WorkspaceId:
        return self._workspace_id
