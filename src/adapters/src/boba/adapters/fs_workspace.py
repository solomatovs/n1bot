"""Файловая реализация FileStorage."""

from __future__ import annotations

import os
import shutil
from collections.abc import Iterator
from datetime import datetime
from io import BufferedIOBase, TextIOBase
from pathlib import Path
from threading import Lock

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

    def _ensure_created(self, path):
        resolved = self._resolve(path)
        resolved.parent.mkdir(parents=True, exist_ok=True)
        return resolved

    def read_lines(
        self, path: str, *, reverse: bool = False, encoding: str = "utf-8"
    ) -> Iterator[str]:
        resolved = self._resolve(path)
        if not reverse:
            with open(resolved, encoding=encoding) as f:
                for line in f:
                    yield line.rstrip("\n")
            return

        with open(resolved, "rb") as f:
            f.seek(0, os.SEEK_END)
            remaining = f.tell()
            if remaining == 0:
                return

            buf = b""
            chunk_size = 8192
            while remaining > 0:
                read_size = min(chunk_size, remaining)
                remaining -= read_size
                f.seek(remaining)
                buf = f.read(read_size) + buf

                while b"\n" in buf:
                    buf, _, line = buf.rpartition(b"\n")
                    decoded = line.decode(encoding)
                    if decoded:
                        yield decoded

            if buf:
                decoded = buf.decode(encoding)
                if decoded:
                    yield decoded

    def read_text(self, path: str, encoding: str = "utf-8") -> TextIOBase:
        return open(self._resolve(path), encoding=encoding)

    def read_binary(self, path: str) -> BufferedIOBase:
        return open(self._resolve(path), "rb")

    def write_text(self, path: str, encoding: str = "utf-8") -> TextIOBase:
        resolved = self._ensure_created(path)
        return open(resolved, "w", encoding=encoding)

    def append_text(self, path: str, encoding: str = "utf-8") -> TextIOBase:
        resolved = self._ensure_created(path)
        return open(resolved, "a", encoding=encoding)

    def exists(self, key: str) -> bool:
        return self._resolve(key).exists()

    def delete(self, key: str) -> None:
        self._resolve(key).unlink()

    def _iter_files(
        self, path: str | None, spec: Specification[str] | None, recursive: bool
    ) -> Iterator[str]:
        base = self._ensure_created(path)

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
    - Существующие папки подхватываются лениво при первом обращении
    """

    def __init__(self, base_dir: Path) -> None:
        self._base_dir = base_dir
        self._lock = Lock()
        self._storages: dict[WorkspaceId, FsWorkspaceService] = {}
        self._base_dir.mkdir(parents=True, exist_ok=True)

    def create(self) -> WorkspaceService:
        ws_id = WorkspaceId.new()
        self._workspace_dir(ws_id).mkdir(parents=True, exist_ok=True)

        storage = FsWorkspaceService(ws_id, self._workspace_dir(ws_id))
        with self._lock:
            self._storages[ws_id] = storage

        return storage

    def get(self, workspace_id: WorkspaceId) -> WorkspaceService:
        with self._lock:
            if workspace_id in self._storages:
                return self._storages[workspace_id]

            path = self._workspace_dir(workspace_id)
            if not path.is_dir():
                raise FileNotFoundError(f"workspace dir not found: {path}")

            storage = FsWorkspaceService(workspace_id, path)
            self._storages[workspace_id] = storage
            return storage

    def delete(self, workspace_id: WorkspaceId) -> None:
        with self._lock:
            self._storages.pop(workspace_id, None)

            path = self._workspace_dir(workspace_id)
            if not path.is_dir():
                raise FileNotFoundError(f"workspace dir not found: {path}")

            shutil.rmtree(path)

    def _workspace_dir(self, workspace_id: WorkspaceId) -> Path:
        return self._base_dir / str(workspace_id.name)
