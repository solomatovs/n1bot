"""Файловая реализация FileStorage."""

from __future__ import annotations

from datetime import datetime
from io import BufferedIOBase, TextIOBase
from pathlib import Path
from threading import Lock
from typing import Iterator
from uuid import UUID, uuid4

from boba.domain.core.patterns import Specification
from boba.domain.core.workspace import (
    FileMeta,
    PathValidator,
    WorkspaceId,
    WorkspaceManager,
    FileStorage,
)


class FsPathValidator(PathValidator):
    """Проверяет что путь не выходит за пределы root."""

    def __init__(self, root: Path) -> None:
        self._root = root.resolve()

    def validate(self, path: str) -> str:
        resolved = (self._root / path).resolve()
        if not resolved.is_relative_to(self._root):
            raise PermissionError(f"Path escapes workspace: {path}")
        
        return str(resolved)


class FsFileStorage(FileStorage):
    """Файловая реализация FileStorage поверх локальной FS."""

    def __init__(self, workspace_id: WorkspaceId, root: Path) -> None:
        self._workspace_id = workspace_id
        self._root = root.resolve()
        self._validator = FsPathValidator(root)

    @property
    def workspace_id(self) -> WorkspaceId:
        return self._workspace_id

    def open_text(self, path: str, encoding: str = "utf-8") -> TextIOBase:
        return open(self._resolve(path), "r", encoding=encoding)

    def open_binary(self, path: str) -> BufferedIOBase:
        return open(self._resolve(path), "rb")

    def exists(self, key: str) -> bool:
        return self._resolve(key).exists()

    def delete(self, key: str) -> None:
        self._resolve(key).unlink()

    def _iter_files(
        self, path: str | None, spec: Specification[str] | None, recursive: bool
    ) -> Iterator[str]:
        base = self._resolve(path) if path else self._root

        if base.is_file():
            rel = str(base.relative_to(self._root))
            if spec is None or spec.is_satisfied_by(rel):
                yield rel
        elif base.is_dir():
            for p in base.rglob("*") if recursive else base.iterdir():
                if p.is_file():
                    rel = str(p.relative_to(self._root))
                    if spec is None or spec.is_satisfied_by(rel):
                        yield rel

    def ls(
        self, path: str | None = None, spec: Specification[str] | None = None
    ) -> Iterator[str]:
        return self._iter_files(path, spec, recursive=False)

    def tree(
        self, path: str | None = None, spec: Specification[str] | None = None
    ) -> Iterator[str]:
        return self._iter_files(path, spec, recursive=True)

    def meta(self, key: str) -> FileMeta:
        resolved = self._resolve(key)
        stat = resolved.stat()
        return FileMeta(
            path=str(resolved.relative_to(self._root)),
            size=stat.st_size,
            modified=datetime.fromtimestamp(stat.st_mtime, tz=None),
        )

    def _resolve(self, path: str) -> Path:
        return Path(self._validator.validate(path))


class FsWorkspaceManager(WorkspaceManager):
    """Управляет workspace'ами на файловой системе.

    - Каждый workspace — папка с UUID-именем внутри base_dir
    - Кеширует FileStorage: один UUID → один экземпляр
    - При старте подхватывает уже существующие папки
    """

    def __init__(self, base_dir: Path) -> None:
        self._base_dir = base_dir
        self._lock = Lock()
        self._storages: dict[UUID, FsFileStorage] = {}
        self._base_dir.mkdir(parents=True, exist_ok=True)
        self._scan_existing()

    def get_or_create(self, workspace_id: UUID | None = None) -> FileStorage:
        if workspace_id is None:
            workspace_id = uuid4()
            self._make_dir(workspace_id)

        with self._lock:
            if workspace_id not in self._storages:
                path = self._workspace_dir(workspace_id)
                if not path.is_dir():
                    raise FileNotFoundError(f"workspace dir not found: {path}")
                self._storages[workspace_id] = FsFileStorage(
                    WorkspaceId(workspace_id), path
                )
            return self._storages[workspace_id]

    def _make_dir(self, workspace_id: UUID) -> None:
        self._workspace_dir(workspace_id).mkdir()

    def _workspace_dir(self, workspace_id: UUID) -> Path:
        return self._base_dir / str(workspace_id)

    def _scan_existing(self) -> None:
        for child in self._base_dir.iterdir():
            if child.is_dir():
                try:
                    UUID(child.name)
                except ValueError:
                    continue
