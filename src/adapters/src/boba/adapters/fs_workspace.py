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


@contextmanager
def _map_raw_io_errors(path: str) -> Iterator[None]:
    """Маппер — используется IO-обёртками без ссылки на сервис."""
    try:
        yield
    except WorkspaceError:
        raise
    except FileNotFoundError as e:
        raise WorkspaceNotFoundError(path) from e
    except PermissionError as e:
        raise WorkspacePermissionError(path, reason=str(e)) from e
    except OSError as e:
        raise WorkspaceError(f"I/O error on {path!r}: {e}", path=path) from e


class _ErrorMappedTextIO(TextIOBase):
    """Обёртка над ``TextIOBase``, транслирующая ``OSError`` → ``WorkspaceError``.

    Покрывает все I/O-вызовы (write, read, close, flush, seek, tell и т.п.):
    любая низкоуровневая ошибка диска, прав и т.п. наружу выходит
    единственно в форме ``WorkspaceError`` (и потомков).
    """

    def __init__(self, inner: TextIOBase, path: str) -> None:
        super().__init__()
        self._inner = inner
        self._path = path

    def write(self, s: str, /) -> int:
        with _map_raw_io_errors(self._path):
            return self._inner.write(s)

    def writelines(self, lines: Iterator[str], /) -> None:  # type: ignore[override]
        with _map_raw_io_errors(self._path):
            self._inner.writelines(lines)

    def read(self, size: int | None = -1, /) -> str:
        with _map_raw_io_errors(self._path):
            return self._inner.read(size if size is not None else -1)

    def readline(self, size: int | None = -1, /) -> str:  # type: ignore[override]
        with _map_raw_io_errors(self._path):
            return self._inner.readline(size if size is not None else -1)

    def flush(self) -> None:
        with _map_raw_io_errors(self._path):
            self._inner.flush()

    def close(self) -> None:
        with _map_raw_io_errors(self._path):
            self._inner.close()

    def seek(self, offset: int, whence: int = 0, /) -> int:
        with _map_raw_io_errors(self._path):
            return self._inner.seek(offset, whence)

    def tell(self) -> int:
        with _map_raw_io_errors(self._path):
            return self._inner.tell()

    def truncate(self, size: int | None = None, /) -> int:
        with _map_raw_io_errors(self._path):
            return self._inner.truncate(size)

    def __iter__(self) -> _ErrorMappedTextIO:
        return self

    def __next__(self) -> str:
        with _map_raw_io_errors(self._path):
            return next(self._inner)

    @property
    def closed(self) -> bool:
        return self._inner.closed

    def readable(self) -> bool:
        return self._inner.readable()

    def writable(self) -> bool:
        return self._inner.writable()

    def seekable(self) -> bool:
        return self._inner.seekable()


class _ErrorMappedBinaryIO(BufferedIOBase):
    """Обёртка над ``BufferedIOBase`` — ``OSError`` → ``WorkspaceError``."""

    def __init__(self, inner: BufferedIOBase, path: str) -> None:
        super().__init__()
        self._inner = inner
        self._path = path

    def read(self, size: int | None = -1, /) -> bytes:
        with _map_raw_io_errors(self._path):
            return self._inner.read(size if size is not None else -1)

    def read1(self, size: int = -1, /) -> bytes:
        with _map_raw_io_errors(self._path):
            return self._inner.read1(size)

    def readinto(self, buf: memoryview, /) -> int | None:  # type: ignore[override]
        with _map_raw_io_errors(self._path):
            return self._inner.readinto(buf)

    def write(self, buf: bytes | bytearray | memoryview, /) -> int:  # type: ignore[override]
        with _map_raw_io_errors(self._path):
            return self._inner.write(buf)

    def flush(self) -> None:
        with _map_raw_io_errors(self._path):
            self._inner.flush()

    def close(self) -> None:
        with _map_raw_io_errors(self._path):
            self._inner.close()

    def seek(self, offset: int, whence: int = 0, /) -> int:
        with _map_raw_io_errors(self._path):
            return self._inner.seek(offset, whence)

    def tell(self) -> int:
        with _map_raw_io_errors(self._path):
            return self._inner.tell()

    def truncate(self, size: int | None = None, /) -> int:
        with _map_raw_io_errors(self._path):
            return self._inner.truncate(size)

    @property
    def closed(self) -> bool:
        return self._inner.closed

    def readable(self) -> bool:
        return self._inner.readable()

    def writable(self) -> bool:
        return self._inner.writable()

    def seekable(self) -> bool:
        return self._inner.seekable()


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
        with _map_raw_io_errors(path):
            yield

    def mkdir(self, path: str) -> None:
        with self._map_errors(path):
            self._resolve(path).mkdir(parents=True, exist_ok=True)

    def _ensure_created(self, path: str) -> Path:
        resolved = self._resolve(path)
        resolved.parent.mkdir(parents=True, exist_ok=True)
        return resolved

    def _open_for_write(
        self, resolved: Path, mode: str, encoding: str | None = None
    ) -> TextIOBase:
        """Открыть файл для записи; если parent-директории нет — создать её
        и повторить ровно один раз. На happy-path никаких лишних syscalls.
        """
        try:
            return open(resolved, mode, encoding=encoding)  # type: ignore[return-value]
        except FileNotFoundError:
            resolved.parent.mkdir(parents=True, exist_ok=True)
            return open(resolved, mode, encoding=encoding)  # type: ignore[return-value]

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
                    raise WorkspaceError(f"cannot read {path!r}: {e}", path=path) from e

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
            return _ErrorMappedTextIO(
                open(self._resolve(path), encoding=encoding),  # type: ignore[arg-type]
                path,
            )

    def read_binary(self, path: str) -> BufferedIOBase:
        with self._map_errors(path):
            return _ErrorMappedBinaryIO(
                open(self._resolve(path), "rb"),  # type: ignore[arg-type]
                path,
            )

    def write_text(self, path: str, encoding: str = "utf-8") -> TextIOBase:
        with self._map_errors(path):
            return _ErrorMappedTextIO(
                self._open_for_write(self._resolve(path), "w", encoding=encoding),
                path,
            )

    def append_text(self, path: str, encoding: str = "utf-8") -> TextIOBase:
        with self._map_errors(path):
            return _ErrorMappedTextIO(
                self._open_for_write(self._resolve(path), "a", encoding=encoding),
                path,
            )

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
                if spec is None or spec.check(rel):
                    yield rel
            elif base.is_dir():
                for p in base.rglob("*") if recursive else base.iterdir():
                    if p.is_file():
                        rel = str(p.relative_to(self._root))
                        if spec is None or spec.check(rel):
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
