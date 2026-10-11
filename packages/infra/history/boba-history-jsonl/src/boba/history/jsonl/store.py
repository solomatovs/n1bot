"""Реализация HistoryStore над файлом JSONL.

Файл `<root>/<session>.jsonl`, права 0600, каталог 0700
(docs/prompt_assembly/specs/01_journal_write.md).
Захват сессии — flock на файл на всё время работы процесса; второй процесс
получает SessionHeldError. Перед первой дозаписью запечатывается хвост: файл
без перевода строки в конце получает его, иначе новая запись склеилась бы с
оборванной. Пачка пишется одним write без fsync. Чтение — весь файл
построчно: строка не-JSON или не объект отдаётся BrokenLine. Список сессий —
файлы каталога по имени, время — из последней записи файла; удаление —
unlink под тем же flock, так что журнал работающего процесса не удалить.

Ошибки:
HistoryError — каталог или файл недоступны, запись отказала; причина ОС —
    в тексте и в `from exc`.
SessionHeldError — сессию держит другой процесс (подвид HistoryError).
"""

from __future__ import annotations

import asyncio
import fcntl
import json
import logging
import os
import uuid
from collections.abc import AsyncIterator, Iterator, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import ClassVar

from pydantic import ValidationError

from boba.agent.history import (
    BrokenLine,
    HistoryError,
    HistoryLine,
    HistoryStore,
    LineKey,
    SessionHeldError,
    SessionId,
    SessionLease,
    StoredLine,
    StoredPage,
    StoredSession,
)
from boba.agent.records import Record, RecordCodec

logger = logging.getLogger(__name__)

__all__ = ["JsonlHistoryStore"]


class JsonlLayout(StrEnum):
    """Имена и кодировка файлов журнала."""

    SUFFIX = ".jsonl"
    ENCODING = "utf-8"
    PATTERN = "*.jsonl"

    def file_of(self, root: Path, session: SessionId) -> Path:
        return root / f"{session.value}{JsonlLayout.SUFFIX.value}"


@dataclass
class HeldSession:
    """Захваченная сессия: дескриптор замка, путь, последний сохранённый seq."""

    fd: int
    path: Path
    last_seq: int
    sealed: bool = False
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class JsonlHistoryStore(HistoryStore):
    """Реализация HistoryStore над каталогом файлов JSONL."""

    DIR_MODE: ClassVar[int] = 0o700
    FILE_MODE: ClassVar[int] = 0o600
    NEWLINE: ClassVar[bytes] = b"\n"

    def __init__(self, root: Path) -> None:
        self._root = root
        self._codec = RecordCodec()
        self._layout = JsonlLayout.SUFFIX
        self._held: dict[str, HeldSession] = {}

    async def acquire(self, session: SessionId) -> SessionLease:
        path = self._layout.file_of(self._root, session)
        held = await asyncio.to_thread(self._lock, session, path)
        lease = SessionLease(session=session, key=uuid.uuid4().hex)
        self._held[lease.key] = held

        return lease

    async def append(self, lease: SessionLease, records: Sequence[Record]) -> None:
        held = self._held_of(lease)
        async with held.lock:
            fresh = list(self._fresh(held, records))
            if not fresh:
                return

            lines: list[str] = []
            for record in fresh:
                lines.append(self._codec.line(record))

            data = ("\n".join(lines) + "\n").encode(JsonlLayout.ENCODING.value)
            await asyncio.to_thread(self._write, held, data)
            held.last_seq = fresh[-1].seq

    async def read(self, session: SessionId) -> AsyncIterator[HistoryLine]:
        path = self._layout.file_of(self._root, session)
        data = await asyncio.to_thread(self._read_bytes, path)
        for line in self._lines(data):
            yield line

    async def release(self, lease: SessionLease) -> None:
        held = self._held.pop(lease.key, None)
        if held is None:
            return

        try:
            fcntl.flock(held.fd, fcntl.LOCK_UN)
            os.close(held.fd)
        except OSError as exc:
            msg = f"Could not release the lock on the journal {held.path}: {exc}"
            raise HistoryError(msg) from exc

    async def list(self, cursor: str | None, limit: int) -> StoredPage:
        return await asyncio.to_thread(self._list, cursor, limit)

    async def delete(self, session: SessionId) -> None:
        path = self._layout.file_of(self._root, session)
        await asyncio.to_thread(self._unlink, session, path)

    def _list(self, cursor: str | None, limit: int) -> StoredPage:
        sessions: list[StoredSession] = []
        for path in self._journals(cursor):
            if len(sessions) > limit:
                break

            stored = self._stored_session(path)
            if stored is not None:
                sessions.append(stored)

        next_cursor: str | None = None
        if len(sessions) > limit:
            sessions = sessions[:limit]
            next_cursor = sessions[-1].session.value

        return StoredPage(sessions=sessions, next_cursor=next_cursor)

    def _journals(self, cursor: str | None) -> Iterator[Path]:
        """Файлы журналов по возрастанию имени сессии после курсора."""
        if not self._root.is_dir():
            return

        for path in sorted(self._root.glob(JsonlLayout.PATTERN.value)):
            if cursor is not None and path.stem <= cursor:
                continue

            yield path

    def _stored_session(self, path: Path) -> StoredSession | None:
        """Сессия файла с временем последней записи; None — имя файла не
        идентификатор сессии или записей в файле нет."""
        try:
            session = SessionId(value=path.stem)
        except ValidationError:
            logger.warning("history: %s is not a session journal, skipped", path)
            return None

        updated_at: str | None = None
        for line in self._lines(self._read_bytes(path)):
            if not isinstance(line, StoredLine):
                continue

            stamp = line.raw.get(LineKey.TIMESTAMP.value)
            if isinstance(stamp, str):
                updated_at = stamp

        if updated_at is None:
            return None

        return StoredSession(session=session, updated_at=updated_at)

    def _unlink(self, session: SessionId, path: Path) -> None:
        """Ошибки:
        SessionHeldError — журнал под замком работающего процесса.
        HistoryError — файл не открыть или не удалить.
        """
        try:
            fd = os.open(path, os.O_RDONLY)
        except FileNotFoundError:
            return
        except OSError as exc:
            msg = f"Could not open the journal {path} for deletion: {exc}"
            raise HistoryError(msg) from exc

        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            os.close(fd)
            msg = (
                f"Session '{session.value}' cannot be deleted: a running agent "
                f"process holds the lock on the journal {path}. Close that session "
                "first."
            )
            raise SessionHeldError(msg) from exc
        except OSError as exc:
            os.close(fd)
            msg = f"Could not lock the journal {path} for deletion: {exc}"
            raise HistoryError(msg) from exc

        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            msg = f"Could not delete the journal {path}: {exc}"
            raise HistoryError(msg) from exc
        finally:
            os.close(fd)

    def _held_of(self, lease: SessionLease) -> HeldSession:
        held = self._held.get(lease.key)
        if held is None:
            msg = (
                f"Session '{lease.session.value}' was not acquired through this "
                "history store, so its lease is unknown here. Acquire the session "
                "first."
            )
            raise HistoryError(msg)

        return held

    def _fresh(self, held: HeldSession, records: Sequence[Record]) -> Iterator[Record]:
        """Записи с seq новее сохранённого, строго по возрастанию."""
        last = held.last_seq
        for record in records:
            if record.seq <= held.last_seq:
                continue

            if record.seq <= last:
                msg = (
                    f"Cannot append to the journal {held.path}: the batch is out of "
                    f"order, record seq {record.seq} comes after seq {last}."
                )
                raise HistoryError(msg)

            last = record.seq
            yield record

    def _lock(self, session: SessionId, path: Path) -> HeldSession:
        try:
            self._root.mkdir(mode=self.DIR_MODE, parents=True, exist_ok=True)
            fd = os.open(path, os.O_RDONLY | os.O_CREAT, self.FILE_MODE)
        except OSError as exc:
            msg = f"Could not open the journal {path}: {exc}"
            raise HistoryError(msg) from exc

        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            os.close(fd)
            msg = (
                f"Session '{session.value}' is already in use by another process: the "
                f"journal {path} is locked. Stop that process or use another session."
            )
            raise SessionHeldError(msg) from exc
        except OSError as exc:
            os.close(fd)
            msg = f"Could not lock the journal {path}: {exc}"
            raise HistoryError(msg) from exc

        return HeldSession(fd=fd, path=path, last_seq=self._last_seq(path))

    def _last_seq(self, path: Path) -> int:
        last = 0
        for line in self._lines(self._read_bytes(path)):
            if not isinstance(line, StoredLine):
                continue

            seq = line.raw.get(LineKey.SEQ.value)
            if isinstance(seq, int) and seq > last:
                last = seq

        return last

    def _write(self, held: HeldSession, data: bytes) -> None:
        try:
            fd = os.open(held.path, os.O_WRONLY | os.O_APPEND)
        except OSError as exc:
            msg = f"Could not open the journal {held.path} for appending: {exc}"
            raise HistoryError(msg) from exc

        try:
            if not held.sealed:
                self._seal(held, fd)

            os.write(fd, data)
        except OSError as exc:
            msg = f"Could not append {len(data)} bytes to {held.path}: {exc}"
            raise HistoryError(msg) from exc
        finally:
            os.close(fd)

    def _seal(self, held: HeldSession, fd: int) -> None:
        """Файл без перевода строки в конце получает его перед первой дозаписью;
        хвост читается через дескриптор замка, открытый на чтение."""
        size = os.fstat(held.fd).st_size
        if size > 0:
            tail = os.pread(held.fd, 1, size - 1)
            if tail != self.NEWLINE:
                os.write(fd, self.NEWLINE)

        held.sealed = True

    def _read_bytes(self, path: Path) -> bytes:
        if not path.exists():
            return b""

        try:
            return path.read_bytes()
        except OSError as exc:
            msg = f"Could not read the journal {path}: {exc}"
            raise HistoryError(msg) from exc

    def _lines(self, data: bytes) -> Iterator[HistoryLine]:
        for number, raw in enumerate(data.split(self.NEWLINE), start=1):
            if not raw.strip():
                continue

            yield self._line(number, raw)

    def _line(self, number: int, raw: bytes) -> HistoryLine:
        try:
            document = json.loads(raw.decode(JsonlLayout.ENCODING.value))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            return BrokenLine(number=number, reason=f"not JSON: {exc}")

        if not isinstance(document, dict):
            got = type(document).__name__
            reason = f"expected a JSON object, got {got}"

            return BrokenLine(number=number, reason=reason)

        return StoredLine(number=number, raw=document)
