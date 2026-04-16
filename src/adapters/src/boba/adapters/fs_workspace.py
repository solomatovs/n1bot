"""Файловая реализация FileStorage."""

from __future__ import annotations

import shutil
from collections.abc import Iterator
from datetime import datetime
from io import BufferedIOBase, TextIOBase
from pathlib import Path
from threading import Lock
from uuid import UUID, uuid4

from boba.domain.core.patterns import Specification, Validator
from boba.domain.core.workspace import (
    FileMeta,
    WorkspaceId,
    WorkspaceManager,
    WorkspaceService,
)


class FsPathValidator(Validator[str]):
    """Проверяет что путь не выходит за пределы root."""

    def __init__(self, root: Path) -> None:
        self._root = root.resolve()

    def validate(self, path: str) -> str:
        resolved = (self._root / path).resolve()
        if not resolved.is_relative_to(self._root):
            raise PermissionError(f"Path escapes workspace: {path}")

        return str(resolved)


class FsWorkspaceService(WorkspaceService):
    """Файловая реализация WorkspaceService поверх локальной FS."""

    def __init__(self, workspace_id: WorkspaceId, root: Path) -> None:
        self._workspace_id = workspace_id
        self._root = root.resolve()
        self._validator = FsPathValidator(root)

    @property
    def workspace_id(self) -> WorkspaceId:
        return self._workspace_id

    def mkdir(self, path: str) -> None:
        self._resolve(path).mkdir(parents=True, exist_ok=True)

    def read_text(self, path: str, encoding: str = "utf-8") -> TextIOBase:
        return open(self._resolve(path), encoding=encoding)

    def read_binary(self, path: str) -> BufferedIOBase:
        return open(self._resolve(path), "rb")

    def write_text(self, path: str, encoding: str = "utf-8") -> TextIOBase:
        resolved = self._resolve(path)
        resolved.parent.mkdir(parents=True, exist_ok=True)
        return open(resolved, "w", encoding=encoding)

    def append_text(self, path: str, encoding: str = "utf-8") -> TextIOBase:
        resolved = self._resolve(path)
        resolved.parent.mkdir(parents=True, exist_ok=True)
        return open(resolved, "a", encoding=encoding)

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
        self._storages: dict[UUID, FsWorkspaceService] = {}
        self._base_dir.mkdir(parents=True, exist_ok=True)
        self._scan_existing()

    def create(self) -> WorkspaceService:
        ws_id = uuid4()
        self._make_dir(ws_id)

        storage = FsWorkspaceService(WorkspaceId(ws_id), self._workspace_dir(ws_id))
        with self._lock:
            self._storages[ws_id] = storage

        return storage

    def get(self, workspace_id: WorkspaceId) -> WorkspaceService:
        uid = workspace_id.name
        with self._lock:
            if uid in self._storages:
                return self._storages[uid]

            path = self._workspace_dir(uid)
            if not path.is_dir():
                raise FileNotFoundError(f"workspace dir not found: {path}")

            self._storages[uid] = FsWorkspaceService(workspace_id, path)

            return self._storages[uid]

    def delete(self, workspace_id: WorkspaceId) -> None:
        uid = workspace_id.name
        with self._lock:
            self._storages.pop(uid, None)

            path = self._workspace_dir(uid)
            if not path.is_dir():
                raise FileNotFoundError(f"workspace dir not found: {path}")

            shutil.rmtree(path)

    def _make_dir(self, workspace_id: UUID) -> None:
        self._workspace_dir(workspace_id).mkdir()

    def _workspace_dir(self, workspace_id: UUID) -> Path:
        return self._base_dir / str(workspace_id)

    def _scan_existing(self) -> None:
        for child in self._base_dir.iterdir():
            if not child.is_dir():
                continue

            try:
                UUID(child.name)
            except ValueError:
                continue
