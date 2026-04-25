"""Файловая реализация :class:`WorkspaceShell` и :class:`WorkspaceRegistry`."""

from __future__ import annotations

import fnmatch
import logging
import os
import re
import shutil
import stat as stat_mod
import tempfile
from collections import deque
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from io import BufferedIOBase, TextIOBase
from pathlib import Path
from threading import Lock
from typing import Generic, TypeVar

from boba.adapters.growbuffer import GrowBuffer
from boba.domain.core.patterns import Id, Specification
from boba.domain.core.workspace import (
    EntryMeta,
    GrepMatch,
    HistoryWorkspaceRegistry,
    HistoryWorkspaceShell,
    ProjectWorkspaceRegistry,
    ProjectWorkspaceShell,
    ScratchWorkspaceRegistry,
    ScratchWorkspaceShell,
    WorkspaceDecodingError,
    WorkspaceError,
    WorkspaceId,
    WorkspaceNotFoundError,
    WorkspacePermissionError,
    WorkspaceRegistry,
    WorkspaceShell,
)

TWsId = TypeVar("TWsId", bound=Id)

logger = logging.getLogger(__name__)


def _contain_within_root(
    path: str,
    cwd_parts: tuple[str, ...] = (),
) -> str:
    """Нормализует ввод к пути внутри workspace (containment ``..``).

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
class WorkspacePath:
    """Три формы одного пути, обработанного :meth:`FsWorkspaceShell._resolve`.

    * ``source`` — исходный ввод как есть (для логов и диагностики).
    * ``relative`` — путь относительно корня workspace, безопасно
      показывать пользователю в ошибках (не раскрывает реальный путь).
    * ``absolute`` — физический путь на диске, для I/O и логов.
    """

    source: str
    relative: str
    absolute: Path


@contextmanager
def _translate_os_errors(resolved: WorkspacePath) -> Iterator[None]:
    """Переводит низкоуровневые ``OSError`` в иерархию ``WorkspaceError``.

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
            resolved.source,
            resolved.absolute,
        )
        raise WorkspaceNotFoundError(resolved.relative) from e
    except PermissionError as e:
        logger.debug(
            "fs workspace permission denied: source=%r absolute=%s reason=%s",
            resolved.source,
            resolved.absolute,
            e,
        )
        raise WorkspacePermissionError(resolved.relative, reason=str(e)) from e
    except OSError as e:
        logger.debug(
            "fs workspace I/O error: source=%r absolute=%s err=%s",
            resolved.source,
            resolved.absolute,
            e,
        )
        raise WorkspaceError(
            f"I/O error on {resolved.relative!r}: {e}",
            path=resolved.relative,
        ) from e


class _WorkspaceTextStream(TextIOBase):
    """Текстовый stream внутри workspace.

    Покрывает все I/O-вызовы (write, read, close, flush, seek, tell и т.п.):
    любая низкоуровневая ошибка диска/прав наружу выходит единственно в
    форме ``WorkspaceError`` (и потомков). Привязан к ``WorkspacePath``,
    чтобы отдавать пользователю относительный путь, а в логи — source +
    абсолютный.
    """

    def __init__(self, inner: TextIOBase, resolved: WorkspacePath) -> None:
        super().__init__()
        self._inner = inner
        self._resolved = resolved

    def write(self, s: str, /) -> int:
        with _translate_os_errors(self._resolved):
            return self._inner.write(s)

    def writelines(self, lines: Iterator[str], /) -> None:  # type: ignore[override]
        with _translate_os_errors(self._resolved):
            self._inner.writelines(lines)

    def read(self, size: int | None = -1, /) -> str:
        with _translate_os_errors(self._resolved):
            return self._inner.read(size if size is not None else -1)

    def readline(self, size: int | None = -1, /) -> str:  # type: ignore[override]
        with _translate_os_errors(self._resolved):
            return self._inner.readline(size if size is not None else -1)

    def flush(self) -> None:
        with _translate_os_errors(self._resolved):
            self._inner.flush()

    def close(self) -> None:
        with _translate_os_errors(self._resolved):
            self._inner.close()

    def seek(self, offset: int, whence: int = 0, /) -> int:
        with _translate_os_errors(self._resolved):
            return self._inner.seek(offset, whence)

    def tell(self) -> int:
        with _translate_os_errors(self._resolved):
            return self._inner.tell()

    def truncate(self, size: int | None = None, /) -> int:
        with _translate_os_errors(self._resolved):
            return self._inner.truncate(size)

    def __iter__(self) -> _WorkspaceTextStream:
        return self

    def __next__(self) -> str:
        with _translate_os_errors(self._resolved):
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


class _WorkspaceBinaryStream(BufferedIOBase):
    """Бинарный stream внутри workspace — ``OSError`` → ``WorkspaceError``."""

    def __init__(self, inner: BufferedIOBase, resolved: WorkspacePath) -> None:
        super().__init__()
        self._inner = inner
        self._resolved = resolved

    def read(self, size: int | None = -1, /) -> bytes:
        with _translate_os_errors(self._resolved):
            return self._inner.read(size if size is not None else -1)

    def read1(self, size: int = -1, /) -> bytes:
        with _translate_os_errors(self._resolved):
            return self._inner.read1(size)

    def readinto(self, buf: memoryview, /) -> int | None:  # type: ignore[override]
        with _translate_os_errors(self._resolved):
            return self._inner.readinto(buf)

    def write(self, buf: bytes | bytearray | memoryview, /) -> int:  # type: ignore[override]
        with _translate_os_errors(self._resolved):
            return self._inner.write(buf)

    def flush(self) -> None:
        with _translate_os_errors(self._resolved):
            self._inner.flush()

    def close(self) -> None:
        with _translate_os_errors(self._resolved):
            self._inner.close()

    def seek(self, offset: int, whence: int = 0, /) -> int:
        with _translate_os_errors(self._resolved):
            return self._inner.seek(offset, whence)

    def tell(self) -> int:
        with _translate_os_errors(self._resolved):
            return self._inner.tell()

    def truncate(self, size: int | None = None, /) -> int:
        with _translate_os_errors(self._resolved):
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


class FsWorkspaceShell(WorkspaceShell[TWsId], Generic[TWsId]):
    """Файловый shell с фиксированным корнем ``root``.

    Все пути нормализуются через :meth:`_resolve` в :class:`WorkspacePath`,
    где хранится и исходный ввод (для логов), и путь относительно
    workspace (для ошибок пользователю), и физический путь на диске
    (для I/O и диагностики).
    """

    def __init__(
        self,
        workspace_id: TWsId,
        root: Path,
    ) -> None:
        self._workspace_id = workspace_id
        self._root = root.resolve()
        self._separator = b"\n"
        self._cwd_parts: tuple[str, ...] = ()

    @property
    def workspace_id(self) -> TWsId:
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
    def _map_errors(self, resolved: WorkspacePath) -> Iterator[None]:
        """Мапит низкоуровневые исключения в иерархию WorkspaceError."""
        with _translate_os_errors(resolved):
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

    def _ensure_created(self, resolved: WorkspacePath) -> Path:
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
            return _WorkspaceTextStream(
                open(resolved.absolute, encoding=encoding),  # type: ignore[arg-type]
                resolved,
            )

    def read_binary(self, path: str) -> BufferedIOBase:
        resolved = self._resolve(path)
        with self._map_errors(resolved):
            return _WorkspaceBinaryStream(
                open(resolved.absolute, "rb"),  # type: ignore[arg-type]
                resolved,
            )

    def write_text(self, path: str, encoding: str = "utf-8") -> TextIOBase:
        resolved = self._resolve(path)
        with self._map_errors(resolved):
            return _WorkspaceTextStream(
                self._open_for_write(resolved.absolute, "w", encoding=encoding),
                resolved,
            )

    def append_text(self, path: str, encoding: str = "utf-8") -> TextIOBase:
        resolved = self._resolve(path)
        with self._map_errors(resolved):
            return _WorkspaceTextStream(
                self._open_for_write(resolved.absolute, "a", encoding=encoding),
                resolved,
            )

    def write_binary(self, path: str) -> BufferedIOBase:
        resolved = self._resolve(path)
        with self._map_errors(resolved):
            try:
                fp = open(resolved.absolute, "wb")  # noqa: SIM115
            except FileNotFoundError:
                resolved.absolute.parent.mkdir(parents=True, exist_ok=True)
                fp = open(resolved.absolute, "wb")  # noqa: SIM115
            return _WorkspaceBinaryStream(fp, resolved)  # type: ignore[arg-type]

    def grep(  # noqa: PLR0913 — все параметры — независимые флаги grep'а
        self,
        pattern: str,
        path: str | None = None,
        *,
        recursive: bool = True,
        include: str | None = None,
        case_insensitive: bool = False,
        context: int = 0,
        limit: int = 100,
        fixed_string: bool = False,
        encoding: str = "utf-8",
    ) -> Iterator[GrepMatch]:
        compiled = self._compile_grep_pattern(
            pattern,
            fixed_string=fixed_string,
            case_insensitive=case_insensitive,
        )
        start = self._resolve(path or "")
        with self._map_errors(start):
            if not start.absolute.exists():
                raise WorkspaceNotFoundError(start.relative)
        return self._grep_iter(
            compiled,
            start,
            recursive=recursive,
            include=include,
            context=context,
            limit=limit,
            encoding=encoding,
        )

    @staticmethod
    def _compile_grep_pattern(
        pattern: str,
        *,
        fixed_string: bool,
        case_insensitive: bool,
    ) -> re.Pattern[str]:
        raw = re.escape(pattern) if fixed_string else pattern
        flags = re.IGNORECASE if case_insensitive else 0
        try:
            return re.compile(raw, flags)
        except re.error as e:
            raise WorkspaceError(
                f"invalid regex {pattern!r}: {e.msg} at position {e.pos}; "
                f"escape special chars or pass fixed_string=true",
                path=pattern,
            ) from e

    def _grep_iter(  # noqa: PLR0913
        self,
        compiled: re.Pattern[str],
        start: WorkspacePath,
        *,
        recursive: bool,
        include: str | None,
        context: int,
        limit: int,
        encoding: str,
    ) -> Iterator[GrepMatch]:
        if start.absolute.is_file():
            files: Iterator[str] = iter([start.relative])
        elif recursive:
            files = self.tree(start.source or None)
        else:
            files = self.ls(start.source or None)

        found = 0
        for rel in files:
            if found >= limit:
                break
            if include and not fnmatch.fnmatch(rel, include):
                continue
            abs_path = self._root / rel
            if self._is_probably_binary(abs_path):
                continue
            for match in self._grep_file(
                abs_path,
                rel,
                compiled,
                context=context,
                encoding=encoding,
            ):
                yield match
                found += 1
                if found >= limit:
                    break

    @staticmethod
    def _is_probably_binary(abs_path: Path, sniff: int = 8192) -> bool:
        try:
            with open(abs_path, "rb") as f:
                return b"\0" in f.read(sniff)
        except OSError:
            return True

    @staticmethod
    def _grep_file(
        abs_path: Path,
        rel_path: str,
        compiled: re.Pattern[str],
        *,
        context: int,
        encoding: str,
    ) -> Iterator[GrepMatch]:
        before: deque[str] = deque(maxlen=context if context > 0 else 0)
        pending: list[dict] = []  # {"line", "content", "before", "after"}
        try:
            with open(abs_path, encoding=encoding) as f:
                for i, raw in enumerate(f, start=1):
                    line = raw.rstrip("\r\n")
                    still_pending: list[dict] = []
                    for p in pending:
                        p["after"].append(line)
                        if len(p["after"]) >= context:
                            yield GrepMatch(
                                path=rel_path,
                                line=p["line"],
                                content=p["content"],
                                before=tuple(p["before"]),
                                after=tuple(p["after"]),
                            )
                        else:
                            still_pending.append(p)
                    pending = still_pending
                    if compiled.search(line):
                        if context == 0:
                            yield GrepMatch(
                                path=rel_path,
                                line=i,
                                content=line,
                                before=(),
                                after=(),
                            )
                        else:
                            pending.append(
                                {
                                    "line": i,
                                    "content": line,
                                    "before": tuple(before),
                                    "after": [],
                                }
                            )
                    before.append(line)
                for p in pending:
                    yield GrepMatch(
                        path=rel_path,
                        line=p["line"],
                        content=p["content"],
                        before=tuple(p["before"]),
                        after=tuple(p["after"]),
                    )
        except UnicodeDecodeError:
            return

    def edit_text(
        self,
        path: str,
        old: str,
        new: str,
        *,
        replace_all: bool = False,
        encoding: str = "utf-8",
    ) -> int:
        if not old:
            raise WorkspaceError(
                "old_string must not be empty",
                path=path,
            )
        resolved = self._resolve(path)
        with self._map_errors(resolved):
            if not resolved.absolute.exists():
                raise WorkspaceNotFoundError(resolved.relative)
            try:
                count = self._count_stream(resolved.absolute, old, encoding)
            except UnicodeDecodeError as e:
                raise WorkspaceDecodingError(
                    resolved.relative,
                    encoding,
                    e,
                ) from e
            if count == 0:
                raise WorkspaceError(
                    f"old_string not found in {resolved.relative!r}; "
                    f"re-read the file and copy the exact substring",
                    path=resolved.relative,
                )
            if count > 1 and not replace_all:
                raise WorkspaceError(
                    f"old_string matches {count} times in "
                    f"{resolved.relative!r}; add more surrounding context "
                    f"to make it unique, or set replace_all=true",
                    path=resolved.relative,
                )
            applied = count if replace_all else 1
            self._replace_stream(
                resolved.absolute,
                old,
                new,
                applied,
                encoding,
            )
            return applied

    @staticmethod
    def _count_stream(src: Path, old: str, encoding: str) -> int:
        """Потоковый подсчёт вхождений ``old`` с overlap-буфером."""
        old_len = len(old)
        chunk_size = max(65536, old_len * 2)
        keep = old_len - 1
        buffer = ""
        count = 0
        with open(src, encoding=encoding) as f:
            while True:
                data = f.read(chunk_size)
                if not data:
                    break
                buffer += data
                pos = 0
                while True:
                    idx = buffer.find(old, pos)
                    if idx < 0:
                        break
                    count += 1
                    pos = idx + old_len
                # Сохраняем хвост длиной keep: substring может пересечь
                # границу чанков, следующая итерация найдёт его в
                # конкатенации хвост+новый_чанк.
                buffer = buffer[-keep:] if keep else ""
        return count

    @staticmethod
    def _replace_stream(
        target: Path,
        old: str,
        new: str,
        limit: int,
        encoding: str,
    ) -> None:
        """Потоковая замена: src → temp → atomic rename.

        ``limit`` — сколько вхождений заменить (для ``replace_all`` равен
        уже подсчитанному count). Оригинал не модифицируется до rename.
        """
        old_len = len(old)
        chunk_size = max(65536, old_len * 2)
        keep = old_len - 1
        parent = target.parent
        mode = target.stat().st_mode
        fd, tmp_name = tempfile.mkstemp(
            dir=parent,
            prefix=f".{target.name}.",
            suffix=".edit.tmp",
        )
        tmp_path = Path(tmp_name)
        try:
            with (
                open(target, encoding=encoding) as src,
                os.fdopen(fd, "w", encoding=encoding) as dst,
            ):
                buffer = ""
                replaced = 0
                while True:
                    data = src.read(chunk_size)
                    if not data:
                        break
                    buffer += data
                    pos = 0
                    while replaced < limit:
                        idx = buffer.find(old, pos)
                        if idx < 0:
                            break
                        dst.write(buffer[pos:idx])
                        dst.write(new)
                        pos = idx + old_len
                        replaced += 1
                    # Пишем всё безопасное (до last keep символов), остаток
                    # оставляем в buffer для следующей итерации.
                    safe_end = max(pos, len(buffer) - keep)
                    if safe_end > pos:
                        dst.write(buffer[pos:safe_end])
                    buffer = buffer[safe_end:]
                if buffer:
                    dst.write(buffer)
            os.chmod(tmp_path, mode)
            os.replace(tmp_path, target)
        except BaseException:
            try:
                if tmp_path.exists():
                    tmp_path.unlink()
            except OSError:
                pass
            raise

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
                        f"is a directory: {resolved.relative!r} (use recursive=True)",
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

    def meta(self, key: str) -> EntryMeta:
        resolved = self._resolve(key)
        with self._map_errors(resolved):
            st = resolved.absolute.stat()
            if stat_mod.S_ISDIR(st.st_mode):
                kind = "directory"
            elif stat_mod.S_ISREG(st.st_mode):
                kind = "file"
            else:
                kind = "other"

            return EntryMeta(
                path=resolved.relative,
                size=st.st_size,
                modified=datetime.fromtimestamp(st.st_mtime, tz=None),
                kind=kind,
            )

    def _resolve(self, source: str) -> WorkspacePath:
        """Единая точка сборки физического пути.

        Абсолютный ``source`` (с ведущим ``/``) резолвится от корня
        workspace; относительный — от ``cwd``. Клампит ``..``, чтобы путь
        не вышел за корень; после ``Path.resolve()`` проверяет, что
        symlink не увёл наружу. В debug-лог пишет source → absolute для
        диагностики.
        """
        relative = _contain_within_root(source, self._cwd_parts)
        absolute = (self._root / relative).resolve()
        if not absolute.is_relative_to(self._root):
            logger.debug(
                "fs workspace symlink escape: source=%r absolute=%s",
                source,
                absolute,
            )
            raise WorkspacePermissionError(
                relative,
                reason="symlink escapes workspace",
            )
        logger.debug(
            "fs workspace resolved: id=%s source=%r relative=%r absolute=%s",
            self._workspace_id,
            source,
            relative,
            absolute,
        )
        return WorkspacePath(source=source, relative=relative, absolute=absolute)


TWs = TypeVar("TWs", bound=FsWorkspaceShell)


class FsWorkspaceRegistry(WorkspaceRegistry[TWsId], Generic[TWs, TWsId]):
    """Обобщённая файловая реализация реестра.

    Параметризуется классом shell'а ``shell_cls``, ``subdir`` — именем
    подкаталога внутри id-директории, и ``id_factory`` — генератором
    нового id для :meth:`create` (например, :meth:`WorkspaceId.new`
    для UUID-namespace'а или фиксированный конструктор для строкового).
    Маркерные реестры (:class:`FsProjectWorkspaceRegistry` и т.п.) —
    тонкие подклассы, фиксирующие эти параметры; больше в них логики
    не должно быть.
    """

    def __init__(
        self,
        base_dir: Path,
        shell_cls: type[TWs],
        subdir: str,
        id_factory: Callable[[], TWsId],
    ) -> None:
        self._base_dir = base_dir
        self._shell_cls = shell_cls
        self._subdir = subdir
        self._id_factory = id_factory
        self._lock = Lock()
        self._shells: dict[TWsId, TWs] = {}
        self._base_dir.mkdir(parents=True, exist_ok=True)

    def create(self) -> TWs:
        with self._lock:
            ws_id = self._id_factory()
            return self._instantiate(ws_id)

    def get(self, workspace_id: TWsId) -> TWs:
        with self._lock:
            cached = self._shells.get(workspace_id)
            if cached is not None:
                return cached

            path = self._workspace_dir(workspace_id)
            if not path.is_dir():
                raise WorkspaceNotFoundError(str(path))

            shell = self._shell_cls(workspace_id, path)
            self._shells[workspace_id] = shell
            return shell

    def get_or_create(self, workspace_id: TWsId) -> TWs:
        with self._lock:
            cached = self._shells.get(workspace_id)
            if cached is not None:
                return cached
            return self._instantiate(workspace_id)

    def delete(self, workspace_id: TWsId) -> None:
        with self._lock:
            path = self._workspace_dir(workspace_id)
            if path.is_dir():
                shutil.rmtree(path)
            self._shells.pop(workspace_id, None)

    def _instantiate(self, workspace_id: TWsId) -> TWs:
        path = self._workspace_dir(workspace_id)
        path.mkdir(parents=True, exist_ok=True)
        shell = self._shell_cls(workspace_id, path)
        self._shells[workspace_id] = shell
        return shell

    def _workspace_dir(self, workspace_id: TWsId) -> Path:
        return self._base_dir / str(workspace_id.name) / self._subdir


class FsProjectWorkspaceShell(FsWorkspaceShell[WorkspaceId], ProjectWorkspaceShell):
    """Файловый :class:`ProjectWorkspaceShell`."""


class FsHistoryWorkspaceShell(FsWorkspaceShell[WorkspaceId], HistoryWorkspaceShell):
    """Файловый :class:`HistoryWorkspaceShell`."""


class FsScratchWorkspaceShell(FsWorkspaceShell[WorkspaceId], ScratchWorkspaceShell):
    """Файловый :class:`ScratchWorkspaceShell`."""


class FsProjectWorkspaceRegistry(
    FsWorkspaceRegistry[FsProjectWorkspaceShell, WorkspaceId], ProjectWorkspaceRegistry
):
    def __init__(self, base_dir: Path, subdir: str) -> None:
        super().__init__(base_dir, FsProjectWorkspaceShell, subdir, WorkspaceId.new)


class FsHistoryWorkspaceRegistry(
    FsWorkspaceRegistry[FsHistoryWorkspaceShell, WorkspaceId], HistoryWorkspaceRegistry
):
    def __init__(self, base_dir: Path, subdir: str) -> None:
        super().__init__(base_dir, FsHistoryWorkspaceShell, subdir, WorkspaceId.new)


class FsScratchWorkspaceRegistry(
    FsWorkspaceRegistry[FsScratchWorkspaceShell, WorkspaceId], ScratchWorkspaceRegistry
):
    def __init__(self, base_dir: Path, subdir: str) -> None:
        super().__init__(base_dir, FsScratchWorkspaceShell, subdir, WorkspaceId.new)
