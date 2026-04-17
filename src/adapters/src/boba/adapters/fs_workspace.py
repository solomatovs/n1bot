"""Файловая реализация FileStorage."""

from __future__ import annotations

import shutil
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from io import BufferedIOBase, TextIOBase
from pathlib import Path
from threading import Lock

from boba.domain.core.patterns import Specification, Validator
from boba.domain.core.workspace import (
    FileMeta,
    WorkspaceDecodingError,
    WorkspaceError,
    WorkspaceId,
    WorkspaceManager,
    WorkspaceNotFoundError,
    WorkspacePermissionError,
    WorkspaceService,
)
from boba.domain.growbuffer import GrowBuffer


class FsPathValidator(Validator[str]):
    """
    Проверяет что путь не выходит за пределы root
    """

    def __init__(self, root: Path) -> None:
        self._root = root.resolve()

    def validate(self, path: str) -> str:
        resolved = (self._root / path).resolve()
        if not resolved.is_relative_to(self._root):
            raise PermissionError(f"Path escapes workspace: {path}")

        return str(resolved)


class FsWorkspaceService(WorkspaceService):
    """
    """

    def __init__(self, workspace_id: WorkspaceId, root: Path) -> None:
        self._workspace_id = workspace_id
        self._root = root.resolve()
        self._validator = FsPathValidator(root)
        self._separator = b"\n"

    @property
    def workspace_id(self) -> WorkspaceId:
        return self._workspace_id

    @contextmanager
    def _map_errors(self, path: str) -> Iterator[None]:
        """Мапит низкоуровневые исключения в иерархию WorkspaceError."""
        try:
            yield
        except WorkspaceError:
            raise
        except FileNotFoundError as e:
            raise WorkspaceNotFoundError(path) from e
        except PermissionError as e:
            raise WorkspacePermissionError(path, reason=str(e)) from e
        except OSError as e:
            raise WorkspaceError(
                f"I/O error on {path!r}: {e}", path=path
            ) from e

    def mkdir(self, path: str) -> None:
        with self._map_errors(path):
            self._resolve(path).mkdir(parents=True, exist_ok=True)

    def _ensure_created(self, path: str) -> Path:
        resolved = self._resolve(path)
        resolved.parent.mkdir(parents=True, exist_ok=True)
        return resolved

    def read_lines(
        self, path: str, *, reverse: bool = False, encoding: str = "utf-8"
    ) -> Iterator[str]:
        with self._map_errors(path):
            resolved = self._resolve(path)
            with open(resolved, "rb") as f:
                gb = GrowBuffer(f)
                stream = (
                    self._stream_backward(gb, path, encoding)
                    if reverse
                    else self._stream_forward(gb, path, encoding)
                )
                try:
                    yield from stream
                except BufferError as e:
                    raise WorkspaceError(
                        f"cannot read {path!r}: {e}", path=path
                    ) from e

    def _decode(self, raw: bytes, path: str, encoding: str) -> str:
        try:
            return raw.decode(encoding, errors="strict")
        except UnicodeDecodeError as e:
            raise WorkspaceDecodingError(path, encoding, e) from e

    def _stream_forward(
        self, gb: GrowBuffer, path: str, encoding: str
    ) -> Iterator[str]:
        for mv in gb.iter_lines_forward(self._separator, offset=0):
            decoded = self._decode(bytes(mv), path, encoding)
            if decoded:
                yield decoded
        tail = bytes(gb.tail())
        if tail:
            decoded = self._decode(tail, path, encoding)
            if decoded:
                yield decoded

    def _stream_backward(
        self, gb: GrowBuffer, path: str, encoding: str
    ) -> Iterator[str]:
        lines = list(gb.iter_lines_backward(self._separator, offset=0))
        tail = bytes(gb.tail())
        if tail:
            decoded = self._decode(tail, path, encoding)
            if decoded:
                yield decoded
        for mv in lines:
            decoded = self._decode(bytes(mv), path, encoding)
            if decoded:
                yield decoded

    def read_text(self, path: str, encoding: str = "utf-8") -> TextIOBase:
        with self._map_errors(path):
            return open(self._resolve(path), encoding=encoding)

    def read_binary(self, path: str) -> BufferedIOBase:
        with self._map_errors(path):
            return open(self._resolve(path), "rb")

    def write_text(self, path: str, encoding: str = "utf-8") -> TextIOBase:
        with self._map_errors(path):
            resolved = self._ensure_created(path)
            return open(resolved, "w", encoding=encoding)

    def append_text(self, path: str, encoding: str = "utf-8") -> TextIOBase:
        with self._map_errors(path):
            resolved = self._ensure_created(path)
            return open(resolved, "a", encoding=encoding)

    def exists(self, key: str) -> bool:
        with self._map_errors(key):
            return self._resolve(key).exists()

    def delete(self, key: str) -> None:
        with self._map_errors(key):
            self._resolve(key).unlink()

    def _iter_files(
        self, path: str | None, spec: Specification[str] | None, recursive: bool
    ) -> Iterator[str]:
        key = path or ""
        with self._map_errors(key):
            base = self._ensure_created(key)

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
        with self._map_errors(key):
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
    """
    """

    def __init__(self, base_dir: Path) -> None:
        self._base_dir = base_dir
        self._lock = Lock()
        self._storages: dict[WorkspaceId, FsWorkspaceService] = {}
        self._base_dir.mkdir(parents=True, exist_ok=True)

    def create(self) -> WorkspaceService:
        with self._lock:
            ws_id = WorkspaceId.new()
            self._workspace_dir(ws_id).mkdir(parents=True, exist_ok=True)

            storage = FsWorkspaceService(ws_id, self._workspace_dir(ws_id))
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
            self._storages[workspace_id] = FsWorkspaceService(workspace_id, path)

            return storage

    def delete(self, workspace_id: WorkspaceId) -> None:
        with self._lock:
            path = self._workspace_dir(workspace_id)
            if not path.is_dir():
                raise FileNotFoundError(f"workspace dir not found: {path}")

            shutil.rmtree(path)

            self._storages.pop(workspace_id, None)

    def _workspace_dir(self, workspace_id: WorkspaceId) -> Path:
        return self._base_dir / str(workspace_id.name)
