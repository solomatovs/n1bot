"""Файловая реализация WorkspaceManager."""

from __future__ import annotations

from pathlib import Path
from threading import Lock
from uuid import UUID, uuid4

from boba.adapters.fs_workspace_service import FsWorkspaceService
from boba.domain.core.workspace import WorkspaceId, WorkspaceManager, WorkspaceService


class FsWorkspaceManager(WorkspaceManager):
    """Управляет workspace'ами на файловой системе.

    - Каждый workspace — папка с UUID-именем внутри base_dir
    - Кеширует WorkspaceService: один UUID → один экземпляр
    - При старте подхватывает уже существующие папки
    """

    def __init__(self, base_dir: Path) -> None:
        self._base_dir = base_dir
        self._lock = Lock()
        self._services: dict[UUID, FsWorkspaceService] = {}
        self._base_dir.mkdir(parents=True, exist_ok=True)
        self._scan_existing()

    def get_or_create(self, workspace_id: UUID | None = None) -> WorkspaceService:
        if workspace_id is None:
            workspace_id = self._gen_uuid()
            self._create_dir(workspace_id)

        with self._lock:
            if workspace_id not in self._services:
                path = self._get_dir(workspace_id)
                if not path.is_dir():
                    raise FileNotFoundError(f"workspace dir not found: {path}")
                
                self._services[workspace_id] = FsWorkspaceService(
                    WorkspaceId(workspace_id), path
                )
            return self._services[workspace_id]

    def _create_dir(self, workspace_id: UUID) -> UUID:
        self._get_dir(workspace_id).mkdir()
        return workspace_id

    def _gen_uuid(self) -> UUID:
        return uuid4()
    
    def _get_dir(self, workspace_id: UUID) -> Path:
        return self._base_dir / str(workspace_id)

    def _scan_existing(self) -> None:
        for child in self._base_dir.iterdir():
            if child.is_dir():
                try:
                    UUID(child.name)
                except ValueError:
                    continue
