"""Файловая реализация FileStorage."""

from __future__ import annotations

import logging
import shutil
import stat as stat_mod
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from io import BufferedIOBase, TextIOBase
from pathlib import Path
from threading import Lock
from typing import Generic, TypeVar

from boba.adapters.growbuffer import GrowBuffer
from boba.domain.core.patterns import Specification, Validator
from boba.domain.core.workspace import (
    FileMeta,
    SystemWorkspaceManager,
    SystemWorkspaceService,
    TmpWorkspaceManager,
    TmpWorkspaceService,
    UserWorkspaceManager,
    UserWorkspaceService,
    WorkspaceDecodingError,
    WorkspaceError,
    WorkspaceId,
    WorkspaceManager,
    WorkspaceNotFoundError,
    WorkspacePermissionError,
    WorkspaceService,
)

logger = logging.getLogger(__name__)


def _clamp_to_workspace(
    path: str, cwd_parts: tuple[str, ...] = (),
) -> str:
    """Нормализует ввод к пути внутри workspace.

    Абсолютный путь (с ведущим ``/``) разрешается от корня workspace —
    ``cwd_parts`` игнорируется. Относительный — от ``cwd_parts``. ``.``
    пропускается, ``..`` обрабатывается по стеку: если стек пуст (вышли
    бы выше корня) — компонент отбрасывается. Результат не содержит
    ведущего ``/``.
    """
    is_absolute = path.startswith("/")
    stack: list[str] = [] if is_absolute else list(cwd_parts)
    for part in path.replace("\\", "/").split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            if stack:
                stack.pop()
            continue
        stack.append(part)
    return "/".join(stack)


@dataclass(frozen=True)
class _ResolvedPath:
    """Результат нормализации пользовательского пути.

    * ``source`` — исходный ввод как есть (для логов и диагностики).
    * ``relative`` — путь относительно корня workspace, безопасно
      показывать пользователю в ошибках (не раскрывает реальный путь).
    * ``absolute`` — физический путь на диске, для I/O и логов.
    """

    source: str
    relative: str
    absolute: Path


class FsPathValidator(Validator[str]):
    """Приводит пользовательский путь к физическому внутри ``root``.

    Workspace — корневая директория: абсолютный ``/foo`` и относительный
    ``foo`` одинаково ведут в ``root/foo``. ``..`` не выводит выше
    ``root`` — лишние компоненты отбрасываются.

    ``validate`` возвращает абсолютный путь строкой (legacy API). Для
    структурного представления (source/relative/absolute) сервис
    использует ``FsWorkspaceService._resolve`` напрямую.
    """

    def __init__(self, root: Path) -> None:
        self._root = root.resolve()

    def validate(self, path: str) -> str:
        safe = _clamp_to_workspace(path)
        resolved = (self._root / safe).resolve()
        if not resolved.is_relative_to(self._root):
            raise PermissionError(f"Path escapes workspace via symlink: {path}")
        return str(resolved)


@contextmanager
def _map_raw_io_errors(resolved: _ResolvedPath) -> Iterator[None]:
    """Маппер — используется IO-обёртками без ссылки на сервис.

    В публичные ошибки кладётся ``resolved.relative`` (не раскрывает
    реальный путь), а в debug-лог уходят source + absolute для
    диагностики.
    """
    try:
        yield
    except WorkspaceError:
        raise
    except FileNotFoundError as e:
        logger.debug(
            "fs workspace not found: source=%r absolute=%s",
            resolved.source, resolved.absolute,
        )
        raise WorkspaceNotFoundError(resolved.relative) from e
    except PermissionError as e:
        logger.debug(
            "fs workspace permission denied: source=%r absolute=%s reason=%s",
            resolved.source, resolved.absolute, e,
        )
        raise WorkspacePermissionError(resolved.relative, reason=str(e)) from e
    except OSError as e:
        logger.debug(
            "fs workspace I/O error: source=%r absolute=%s err=%s",
            resolved.source, resolved.absolute, e,
        )
        raise WorkspaceError(
            f"I/O error on {resolved.relative!r}: {e}", path=resolved.relative,
        ) from e


class _ErrorMappedTextIO(TextIOBase):
    """Обёртка над ``TextIOBase``, транслирующая ``OSError`` → ``WorkspaceError``.

    Покрывает все I/O-вызовы (write, read, close, flush, seek, tell и т.п.):
    любая низкоуровневая ошибка диска, прав и т.п. наружу выходит
    единственно в форме ``WorkspaceError`` (и потомков). Привязана к
    ``_ResolvedPath``, чтобы отдавать пользователю относительный путь,
    а в логи — source + абсолютный.
    """

    def __init__(self, inner: TextIOBase, resolved: _ResolvedPath) -> None:
        super().__init__()
        self._inner = inner
        self._resolved = resolved

    def write(self, s: str, /) -> int:
        with _map_raw_io_errors(self._resolved):
            return self._inner.write(s)

    def writelines(self, lines: Iterator[str], /) -> None:  # type: ignore[override]
        with _map_raw_io_errors(self._resolved):
            self._inner.writelines(lines)

    def read(self, size: int | None = -1, /) -> str:
        with _map_raw_io_errors(self._resolved):
            return self._inner.read(size if size is not None else -1)

    def readline(self, size: int | None = -1, /) -> str:  # type: ignore[override]
        with _map_raw_io_errors(self._resolved):
            return self._inner.readline(size if size is not None else -1)

    def flush(self) -> None:
        with _map_raw_io_errors(self._resolved):
            self._inner.flush()

    def close(self) -> None:
        with _map_raw_io_errors(self._resolved):
            self._inner.close()

    def seek(self, offset: int, whence: int = 0, /) -> int:
        with _map_raw_io_errors(self._resolved):
            return self._inner.seek(offset, whence)

    def tell(self) -> int:
        with _map_raw_io_errors(self._resolved):
            return self._inner.tell()

    def truncate(self, size: int | None = None, /) -> int:
        with _map_raw_io_errors(self._resolved):
            return self._inner.truncate(size)

    def __iter__(self) -> _ErrorMappedTextIO:
        return self

    def __next__(self) -> str:
        with _map_raw_io_errors(self._resolved):
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

    def __init__(self, inner: BufferedIOBase, resolved: _ResolvedPath) -> None:
        super().__init__()
        self._inner = inner
        self._resolved = resolved

    def read(self, size: int | None = -1, /) -> bytes:
        with _map_raw_io_errors(self._resolved):
            return self._inner.read(size if size is not None else -1)

    def read1(self, size: int = -1, /) -> bytes:
        with _map_raw_io_errors(self._resolved):
            return self._inner.read1(size)

    def readinto(self, buf: memoryview, /) -> int | None:  # type: ignore[override]
        with _map_raw_io_errors(self._resolved):
            return self._inner.readinto(buf)

    def write(self, buf: bytes | bytearray | memoryview, /) -> int:  # type: ignore[override]
        with _map_raw_io_errors(self._resolved):
            return self._inner.write(buf)

    def flush(self) -> None:
        with _map_raw_io_errors(self._resolved):
            self._inner.flush()

    def close(self) -> None:
        with _map_raw_io_errors(self._resolved):
            self._inner.close()

    def seek(self, offset: int, whence: int = 0, /) -> int:
        with _map_raw_io_errors(self._resolved):
            return self._inner.seek(offset, whence)

    def tell(self) -> int:
        with _map_raw_io_errors(self._resolved):
            return self._inner.tell()

    def truncate(self, size: int | None = None, /) -> int:
        with _map_raw_io_errors(self._resolved):
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
    """Файловый workspace с фиксированным корнем ``root``.

    Все пути нормализуются через :meth:`_resolve` в ``_ResolvedPath``,
    где хранится и исходный ввод (для логов), и путь относительно
    workspace (для ошибок пользователю), и физический путь на диске
    (для I/O и диагностики).
    """

    def __init__(
        self,
        workspace_id: WorkspaceId,
        root: Path,
    ) -> None:
        self._workspace_id = workspace_id
        self._root = root.resolve()
        self._separator = b"\n"
        self._cwd_parts: tuple[str, ...] = ()

    @property
    def workspace_id(self) -> WorkspaceId:
        return self._workspace_id

    @property
    def cwd(self) -> str:
        return "/" + "/".join(self._cwd_parts)

    def cd(self, path: str) -> None:
        resolved = self._resolve(path)
        with self._map_errors(resolved):
            if not resolved.absolute.exists():
                raise WorkspaceNotFoundError(resolved.relative)
            if not resolved.absolute.is_dir():
                raise WorkspaceError(
                    f"not a directory: {resolved.relative!r}",
                    path=resolved.relative,
                )
        self._cwd_parts = tuple(p for p in resolved.relative.split("/") if p)

    @contextmanager
    def _map_errors(self, resolved: _ResolvedPath) -> Iterator[None]:
        """Мапит низкоуровневые исключения в иерархию WorkspaceError."""
        with _map_raw_io_errors(resolved):
            yield

    def mkdir(self, path: str) -> None:
        resolved = self._resolve(path)
        with self._map_errors(resolved):
            resolved.absolute.mkdir(parents=True, exist_ok=True)

    def touch(self, path: str) -> None:
        resolved = self._resolve(path)
        with self._map_errors(resolved):
            resolved.absolute.parent.mkdir(parents=True, exist_ok=True)
            resolved.absolute.touch(exist_ok=True)

    def _ensure_created(self, resolved: _ResolvedPath) -> Path:
        resolved.absolute.parent.mkdir(parents=True, exist_ok=True)
        return resolved.absolute

    def _open_for_write(
        self, absolute: Path, mode: str, encoding: str | None = None
    ) -> TextIOBase:
        """Открыть файл для записи; если parent-директории нет — создать её
        и повторить ровно один раз. На happy-path никаких лишних syscalls.
        """
        try:
            return open(absolute, mode, encoding=encoding)  # type: ignore[return-value]
        except FileNotFoundError:
            absolute.parent.mkdir(parents=True, exist_ok=True)
            return open(absolute, mode, encoding=encoding)  # type: ignore[return-value]

    def read_lines(
        self, path: str, *, reverse: bool = False, encoding: str = "utf-8"
    ) -> Iterator[str]:
        resolved = self._resolve(path)
        with self._map_errors(resolved), open(resolved.absolute, "rb") as f:
            gb = GrowBuffer(f)
            stream = (
                self._stream_backward(gb, resolved.relative, encoding)
                if reverse
                else self._stream_forward(gb, resolved.relative, encoding)
            )
            try:
                yield from stream
            except BufferError as e:
                raise WorkspaceError(
                    f"cannot read {resolved.relative!r}: {e}",
                    path=resolved.relative,
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
        resolved = self._resolve(path)
        with self._map_errors(resolved):
            return _ErrorMappedTextIO(
                open(resolved.absolute, encoding=encoding),  # type: ignore[arg-type]
                resolved,
            )

    def read_binary(self, path: str) -> BufferedIOBase:
        resolved = self._resolve(path)
        with self._map_errors(resolved):
            return _ErrorMappedBinaryIO(
                open(resolved.absolute, "rb"),  # type: ignore[arg-type]
                resolved,
            )

    def write_text(self, path: str, encoding: str = "utf-8") -> TextIOBase:
        resolved = self._resolve(path)
        with self._map_errors(resolved):
            return _ErrorMappedTextIO(
                self._open_for_write(resolved.absolute, "w", encoding=encoding),
                resolved,
            )

    def append_text(self, path: str, encoding: str = "utf-8") -> TextIOBase:
        resolved = self._resolve(path)
        with self._map_errors(resolved):
            return _ErrorMappedTextIO(
                self._open_for_write(resolved.absolute, "a", encoding=encoding),
                resolved,
            )

    def exists(self, key: str) -> bool:
        resolved = self._resolve(key)
        with self._map_errors(resolved):
            return resolved.absolute.exists()

    def delete(self, key: str, *, recursive: bool = False) -> None:
        resolved = self._resolve(key)
        with self._map_errors(resolved):
            if not resolved.absolute.exists():
                raise WorkspaceNotFoundError(resolved.relative)
            if resolved.absolute.is_dir():
                if not recursive:
                    raise WorkspaceError(
                        f"is a directory: {resolved.relative!r} "
                        f"(use recursive=True)",
                        path=resolved.relative,
                    )
                shutil.rmtree(resolved.absolute)
            else:
                resolved.absolute.unlink()

    def move(self, src: str, dst: str) -> None:
        src_resolved = self._resolve(src)
        dst_resolved = self._resolve(dst)
        with self._map_errors(src_resolved):
            if not src_resolved.absolute.exists():
                raise WorkspaceNotFoundError(src_resolved.relative)
            shutil.move(str(src_resolved.absolute), str(dst_resolved.absolute))

    def copy(self, src: str, dst: str, *, recursive: bool = False) -> None:
        src_resolved = self._resolve(src)
        dst_resolved = self._resolve(dst)
        with self._map_errors(src_resolved):
            if not src_resolved.absolute.exists():
                raise WorkspaceNotFoundError(src_resolved.relative)

            # cp-совместимая семантика: если dst — существующая
            # директория, копия кладётся внутрь с именем src.
            if dst_resolved.absolute.is_dir():
                final_dst = dst_resolved.absolute / src_resolved.absolute.name
            else:
                final_dst = dst_resolved.absolute

            if src_resolved.absolute.is_dir():
                if not recursive:
                    raise WorkspaceError(
                        f"is a directory: {src_resolved.relative!r} "
                        f"(use recursive=True)",
                        path=src_resolved.relative,
                    )
                shutil.copytree(
                    src_resolved.absolute,
                    final_dst,
                    copy_function=shutil.copyfile,
                    dirs_exist_ok=True,
                )
            else:
                shutil.copyfile(src_resolved.absolute, final_dst)

    def _iter_files(
        self, path: str | None, spec: Specification[str] | None, recursive: bool
    ) -> Iterator[str]:
        resolved = self._resolve(path or "")
        with self._map_errors(resolved):
            base = self._ensure_created(resolved)

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
        resolved = self._resolve(key)
        with self._map_errors(resolved):
            st = resolved.absolute.stat()
            if stat_mod.S_ISDIR(st.st_mode):
                kind = "directory"
            elif stat_mod.S_ISREG(st.st_mode):
                kind = "file"
            else:
                kind = "other"

            return FileMeta(
                path=resolved.relative,
                size=st.st_size,
                modified=datetime.fromtimestamp(st.st_mtime, tz=None),
                kind=kind,
            )

    def _resolve(self, source: str) -> _ResolvedPath:
        """Единая точка сборки физического пути.

        Абсолютный ``source`` (с ведущим ``/``) резолвится от корня
        workspace; относительный — от ``cwd``. Клампит ``..``, чтобы путь
        не вышел за корень; после ``Path.resolve()`` проверяет, что
        symlink не увёл наружу. В debug-лог пишет source → absolute для
        диагностики.
        """
        relative = _clamp_to_workspace(source, self._cwd_parts)
        absolute = (self._root / relative).resolve()
        if not absolute.is_relative_to(self._root):
            logger.debug(
                "fs workspace symlink escape: source=%r absolute=%s",
                source, absolute,
            )
            raise WorkspacePermissionError(
                relative, reason="symlink escapes workspace",
            )
        logger.debug(
            "fs workspace resolved: id=%s source=%r relative=%r absolute=%s",
            self._workspace_id, source, relative, absolute,
        )
        return _ResolvedPath(source=source, relative=relative, absolute=absolute)


TWs = TypeVar("TWs", bound=FsWorkspaceService)


class FsWorkspaceManager(WorkspaceManager, Generic[TWs]):
    """Обобщённая файловая реализация менеджера.

    Параметризуется классом сервиса ``service_cls`` и ``subdir`` — именем
    подкаталога внутри workspace-id директории. Маркерные менеджеры
    (:class:`FsUserWorkspaceManager` и т.п.) — тонкие подклассы,
    фиксирующие эти параметры; больше в них логики не должно быть.
    """

    def __init__(
        self,
        base_dir: Path,
        service_cls: type[TWs],
        subdir: str,
    ) -> None:
        self._base_dir = base_dir
        self._service_cls = service_cls
        self._subdir = subdir
        self._lock = Lock()
        self._storages: dict[WorkspaceId, TWs] = {}
        self._base_dir.mkdir(parents=True, exist_ok=True)

    def create(self) -> TWs:
        with self._lock:
            ws_id = WorkspaceId.new()
            return self._instantiate(ws_id)

    def get(self, workspace_id: WorkspaceId) -> TWs:
        with self._lock:
            cached = self._storages.get(workspace_id)
            if cached is not None:
                return cached

            path = self._workspace_dir(workspace_id)
            if not path.is_dir():
                raise WorkspaceNotFoundError(str(path))

            storage = self._service_cls(workspace_id, path)
            self._storages[workspace_id] = storage
            return storage

    def get_or_create(self, workspace_id: WorkspaceId) -> TWs:
        with self._lock:
            cached = self._storages.get(workspace_id)
            if cached is not None:
                return cached
            return self._instantiate(workspace_id)

    def delete(self, workspace_id: WorkspaceId) -> None:
        with self._lock:
            path = self._workspace_dir(workspace_id)
            if path.is_dir():
                shutil.rmtree(path)
            self._storages.pop(workspace_id, None)

    def _instantiate(self, workspace_id: WorkspaceId) -> TWs:
        path = self._workspace_dir(workspace_id)
        path.mkdir(parents=True, exist_ok=True)
        storage = self._service_cls(workspace_id, path)
        self._storages[workspace_id] = storage
        return storage

    def _workspace_dir(self, workspace_id: WorkspaceId) -> Path:
        return self._base_dir / str(workspace_id.name) / self._subdir


class FsUserWorkspaceService(FsWorkspaceService, UserWorkspaceService):
    """Файловый :class:`UserWorkspaceService`."""


class FsSystemWorkspaceService(FsWorkspaceService, SystemWorkspaceService):
    """Файловый :class:`SystemWorkspaceService`."""


class FsTmpWorkspaceService(FsWorkspaceService, TmpWorkspaceService):
    """Файловый :class:`TmpWorkspaceService`."""


class FsUserWorkspaceManager(
    FsWorkspaceManager[FsUserWorkspaceService], UserWorkspaceManager
):
    def __init__(self, base_dir: Path, subdir: str) -> None:
        super().__init__(base_dir, FsUserWorkspaceService, subdir)


class FsSystemWorkspaceManager(
    FsWorkspaceManager[FsSystemWorkspaceService], SystemWorkspaceManager
):
    def __init__(self, base_dir: Path, subdir: str) -> None:
        super().__init__(base_dir, FsSystemWorkspaceService, subdir)


class FsTmpWorkspaceManager(
    FsWorkspaceManager[FsTmpWorkspaceService], TmpWorkspaceManager
):
    def __init__(self, base_dir: Path, subdir: str) -> None:
        super().__init__(base_dir, FsTmpWorkspaceService, subdir)
