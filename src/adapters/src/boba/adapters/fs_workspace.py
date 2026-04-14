"""Реализация WorkspaceRegistry через файловую систему."""

from __future__ import annotations

from pathlib import Path
from uuid import UUID, uuid4

from boba.domain.core.workspace import (
    WorkspaceBusyError,
    WorkspaceId,
    WorkspaceNotFoundError,
    WorkspaceRegistry,
)


class FsWorkspaceRegistry(WorkspaceRegistry):
    """Каждый workspace — папка с UUID-именем внутри base_dir."""

    def __init__(self, base_dir: Path) -> None:
        self._base_dir = base_dir
        self._workspaces: dict[UUID, WorkspaceId] = {}
        self._base_dir.mkdir(parents=True, exist_ok=True)
        self._scan_existing()

    def create(self) -> WorkspaceId:
        uid = uuid4()
        (self._base_dir / str(uid)).mkdir()
        ws = WorkspaceId(uid)
        self._workspaces[uid] = ws
        return ws

    def get(self, id: UUID) -> WorkspaceId:
        ws = self._workspaces.get(id)
        if ws is None:
            raise WorkspaceNotFoundError(id)
        return ws

    async def delete(self, workspace: WorkspaceId) -> None:
        if workspace.active:
            raise WorkspaceBusyError(workspace.id)

        path = self._base_dir / str(workspace.id)
        if path.exists():
            _rmtree(path)

        self._workspaces.pop(workspace.id, None)

    def _scan_existing(self) -> None:
        for child in self._base_dir.iterdir():
            if child.is_dir():
                try:
                    uid = UUID(child.name)
                except ValueError:
                    continue
                self._workspaces[uid] = WorkspaceId(uid)


def _rmtree(path: Path) -> None:
    """Рекурсивное удаление директории."""
    import shutil

    shutil.rmtree(path)
