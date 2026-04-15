"""Файловая реализация WorkspaceManager."""

from __future__ import annotations

from datetime import datetime
from io import BufferedIOBase, TextIOBase
from pathlib import Path
from threading import Lock
from typing import Iterator
from uuid import UUID, uuid4

from boba.domain.core.workspace import (
    FileMeta,
    PathValidator,
    WorkspaceId,
    WorkspaceManager,
    WorkspaceService,
)


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


class FsPathValidator(PathValidator):
    """Валидатор путей для файловой системы. Проверяет что путь не выходит за пределы root."""

    def __init__(self, root: Path) -> None:
        self._root = root.resolve()

    def validate(self, path: str) -> str:
        resolved = (self._root / path).resolve()
        if not resolved.is_relative_to(self._root):
            raise PermissionError(f"Path escapes workspace: {path}")
        return str(resolved.relative_to(self._root))


class FsWorkspaceService(WorkspaceService):
    """Файловая реализация WorkspaceService."""

    def __init__(self, workspace_id: WorkspaceId, root: Path) -> None:
        self._workspace_id = workspace_id
        self._root = root
        self._validator = FsPathValidator(root)

    @property
    def workspace_id(self) -> WorkspaceId:
        return self._workspace_id

    def open_text(self, path: str, mode: str = "r", encoding: str = "utf-8") -> TextIOBase:
        resolved = self._resolve(path)
        if "w" in mode or "a" in mode:
            resolved.parent.mkdir(parents=True, exist_ok=True)
        return open(resolved, mode, encoding=encoding)  # type: ignore[return-value]

    def open_binary(self, path: str, mode: str = "rb") -> BufferedIOBase:
        resolved = self._resolve(path)
        if "w" in mode or "a" in mode:
            resolved.parent.mkdir(parents=True, exist_ok=True)
        return open(resolved, mode)  # type: ignore[return-value]

    def exists(self, path: str) -> bool:
        return self._resolve(path).exists()

    def delete(self, path: str) -> None:
        self._resolve(path).unlink()

    def rename(self, src: str, dst: str) -> None:
        src_resolved = self._resolve(src)
        dst_resolved = self._resolve(dst)
        dst_resolved.parent.mkdir(parents=True, exist_ok=True)
        src_resolved.rename(dst_resolved)

    def list(self, prefix: str = "", recursive: bool = False) -> Iterator[str]:
        base = self._resolve(prefix) if prefix else self._root
        if not base.is_dir():
            return
        pattern = "**/*" if recursive else "*"
        root_resolved = self._root.resolve()
        for p in base.glob(pattern):
            if p.is_file():
                yield str(p.resolve().relative_to(root_resolved))

    def meta(self, path: str) -> FileMeta:
        resolved = self._resolve(path)
        stat = resolved.stat()
        root_resolved = self._root.resolve()
        return FileMeta(
            path=str(resolved.relative_to(root_resolved)),
            size=stat.st_size,
            modified=datetime.fromtimestamp(stat.st_mtime),
        )

    def _resolve(self, path: str) -> Path:
        validated = self._validator.validate(path)
        return (self._root / validated).resolve()
