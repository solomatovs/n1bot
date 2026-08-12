"""Журнал вывода: файл на каждый исходящий канал стадии в томе пользователя.

Запись идёт по мере работы стадии и не зависит от её исхода: насос кладёт
байты в буфер выделенного writer-потока вызова, файлы пишет он — медленный
диск не двигает дедлайны стадий и не мешает отмене. Чтение — окнами по
произвольному смещению, целиком файл в память не поднимается. Том
пользователя — каталог; общий потолок держит точка монтирования под корнем
журналов. Место кончилось — старейшие закрытые вызовы вытесняются целиком,
пока новый журнал не откроется; вытеснять нечего — канал идёт без журнала,
причина уходит в лог приложения. Сбой журнала (переполнение буфера, ошибка
записи) закрывает файл пометкой и отключает приёмник, поток стадии не рвётся.

Реестр живых вызовов ведёт журнал: он защищает их файлы от вытеснения и
раздаёт события подписчикам (панель UI), не зная о них ничего.

Ошибки:
JournalError — том недоступен, файл журнала не открывается или не читается.
"""

from __future__ import annotations

import errno
import json
import logging
import os
import shutil
import threading
from abc import abstractmethod
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import ClassVar, Protocol

from pydantic import BaseModel, ConfigDict

from boba.sandbox.runner import ToolCallContext
from boba.toolkit.channels import (
    ByteText,
    Channel,
    ChannelError,
    ChannelSink,
    JournalFile,
    StreamKey,
)

__all__ = [
    "CallJournal",
    "CallLogUsage",
    "DirVault",
    "JournalError",
    "JournalListener",
    "JournalNote",
    "JournalRecord",
    "JournalRegistry",
    "JournalSink",
    "JournalWriter",
    "NoJournalSink",
    "StreamFileView",
    "StreamJournal",
    "StreamJournalHub",
    "StreamMeta",
    "StreamSlice",
    "ThreadUsage",
    "VaultUsage",
]

logger = logging.getLogger(__name__)


class JournalError(RuntimeError):
    """Том журнала недоступен: писать или читать некуда."""


class WriteAck(StrEnum):
    """Ответ writer-потока на порцию байтов."""

    OK = "ok"
    OVERFLOW = "buffer overflow"
    STOPPED = "writer stopped"


class JournalNote(StrEnum):
    """Пометка итога журнала канала: попадает в сайдкар и в панель."""

    DONE = ""
    OVERFLOW = "journal stopped: the tool writes faster than the disk"
    STOPPED = "journal stopped: writer is gone"
    ABORTED = "journal stopped: the call ended before the channel"
    WRITE_FAILED = "journal stopped: write failed"

    @classmethod
    def of(cls, ack: WriteAck) -> JournalNote:
        """Пометка по отказу writer-потока."""
        if ack is WriteAck.OVERFLOW:
            return cls.OVERFLOW

        return cls.STOPPED

    @classmethod
    def failed(cls, exc: OSError) -> str:
        reason = exc.strerror
        if not reason:
            reason = str(exc)

        return f"{cls.WRITE_FAILED.value}: {reason}"


class StreamMeta(BaseModel):
    """Сайдкар журнала канала: итог записи; адрес несёт имя файла."""

    model_config = ConfigDict(extra="ignore")

    closed: bool = False
    note: str = ""


class StreamSlice(BaseModel):
    """Окно журнала для показа: текст плюс координаты в файле."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    text: str
    offset: int
    end: int
    """Байт за последним в окне: сюда стыкуется следующее окно."""
    size: int
    window: int
    closed: bool
    note: str


class ThreadUsage(BaseModel):
    """Занятость журналов одного треда."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    thread_id: str
    bytes_used: int
    calls: int
    last_write_at: float


class VaultUsage(BaseModel):
    """Занятость служебного тома пользователя."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    total_bytes: int
    free_bytes: int
    threads: tuple[ThreadUsage, ...]


@dataclass
class _CallTotals:
    """Накопитель обхода: байты, время и файлы одного вызова."""

    bytes_used: int
    last_write_at: float
    files: list[str]


class CallLogUsage(BaseModel):
    """Журналы одного вызова целиком: единица учёта и вытеснения.

    Вызов — это все файлы каналов всех его стадий вместе с сайдкарами: LRU
    сносит их одной записью, иначе у живого показа осталась бы половина
    каналов.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    thread_id: str
    call_id: str
    bytes_used: int
    last_write_at: float
    files: tuple[str, ...]
    """rel-пути всех файлов вызова: логи и сайдкары."""

    @property
    def prefix(self) -> str:
        return StreamKey.prefix_of(self.thread_id, self.call_id)


class DirVault:
    """Том-каталог: без монтирований и привилегий, квоту держит точка монтирования."""

    def __init__(self, root: str) -> None:
        self._root = root

    def ensure_root(self) -> str:
        """Корень тома на старте: журнал обязателен, некуда писать — не стартуем."""
        try:
            os.makedirs(self._root, exist_ok=True)
        except OSError as exc:
            raise JournalError(
                f"stream vault is not writable: {self._root}: {exc}"
            ) from exc

        return self._root

    def root_for(self, user_id: str) -> str:
        path = os.path.join(self._root, user_id)

        try:
            os.makedirs(path, exist_ok=True)
        except OSError as exc:
            raise JournalError(f"stream vault is not writable: {path}: {exc}") from exc

        return path


class StreamFileView:
    """Чтение окон журнала по смещению: файл любого размера, память — окно.

    Окна выравниваются по границам строк, чтобы фронт склеивал их встык без
    рваных строк: голова окна сдвигается за первый перевод строки (кроме
    начала файла), хвост forward-окна обрезается по последнему (кроме конца
    файла). Строка длиннее окна отдаётся как есть — прогресс важнее красоты.
    """

    def __init__(self, log_path: str) -> None:
        self._log_path = log_path

    def slice_at(
        self, offset: int, window: int, size: int, meta: StreamMeta
    ) -> StreamSlice:
        """Окно вперёд от смещения: [выровненный offset, граница строки)."""
        start = max(0, min(offset, size))
        start, data = self._aligned_read(start, min(window, size - start))

        end = start + len(data)
        if end < size:
            cut = data.rfind(b"\n")
            if 0 <= cut < len(data) - 1:
                data = data[: cut + 1]
                end = start + len(data)

        return self._slice(data, start, end, size, window, meta)

    def slice_before(
        self, end: int, window: int, size: int, meta: StreamMeta
    ) -> StreamSlice:
        """Окно, заканчивающееся ровно на end: стык для прокрутки вверх."""
        stop = max(0, min(end, size))
        start = max(0, stop - window)
        start, data = self._aligned_read(start, stop - start)

        return self._slice(data, start, start + len(data), size, window, meta)

    def _aligned_read(self, start: int, length: int) -> tuple[int, bytes]:
        """Чтение с головой на границе строки.

        Смещение, стоящее сразу за переводом строки (стык окон), не трогается;
        произвольное — сдвигается за первый перевод строки внутри окна. Без
        переводов строки вовсе (одна строка длиннее окна) — как есть.
        """
        if length <= 0:
            return start, b""

        if start == 0:
            return start, self._read(0, length)

        raw = self._read(start - 1, length + 1)
        data = raw[1:]
        if raw[:1] == b"\n":
            return start, data

        cut = data.find(b"\n")
        if cut < 0 or cut == len(data) - 1:
            return start, data

        return start + cut + 1, data[cut + 1 :]

    def _read(self, start: int, length: int) -> bytes:
        if length <= 0:
            return b""

        fd = os.open(self._log_path, os.O_RDONLY)
        try:
            return os.pread(fd, length, start)
        finally:
            os.close(fd)

    @staticmethod
    def _slice(  # noqa: PLR0913
        data: bytes,
        start: int,
        end: int,
        size: int,
        window: int,
        meta: StreamMeta,
    ) -> StreamSlice:
        return StreamSlice(
            text=data.decode(ByteText.ENCODING, errors=ByteText.ERRORS),
            offset=start,
            end=end,
            size=size,
            window=window,
            closed=meta.closed,
            note=meta.note,
        )


class JournalRecord:
    """Файл одного канала стадии: дескриптор, размер и сайдкар.

    Пишет только writer-поток вызова; чтение размера и итога идёт из чужих
    потоков, поэтому состояние держит лок.
    """

    FILE_MODE: ClassVar[int] = 0o600

    def __init__(self, key: StreamKey, log_path: str, meta_path: str) -> None:
        self._key = key
        self._log_path = log_path
        self._meta_path = meta_path
        self._lock = threading.Lock()
        self._meta = StreamMeta()
        self._closed = False

        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
        self._fd = os.open(log_path, flags, self.FILE_MODE)
        self._size = os.fstat(self._fd).st_size
        self._write_meta()

    @property
    def key(self) -> StreamKey:
        return self._key

    @property
    def log_path(self) -> str:
        return self._log_path

    @property
    def closed(self) -> bool:
        with self._lock:
            return self._closed

    @property
    def size(self) -> int:
        with self._lock:
            return self._size

    def write(self, data: bytes) -> None:
        """Дописать порцию; зовёт writer-поток, OSError разбирает он же.

        Запись повторяется до конца порции: короткий `os.write` на исходе
        места иначе потерял бы хвост и разошёлся с учтённым размером.
        """
        with self._lock:
            if self._closed:
                return

            view = memoryview(data)
            written = 0

            while written < len(view):
                written += os.write(self._fd, view[written:])

            self._size += written

    def finish(self, note: str) -> None:
        """Идемпотентное закрытие: первый вызов фиксирует итог в сайдкаре."""
        with self._lock:
            if self._closed:
                return

            self._closed = True
            self._meta = StreamMeta(closed=True, note=note)
            os.close(self._fd)

        self._write_meta()

    def tail(self, window: int) -> StreamSlice:
        """Хвост записанного: показ живого канала идёт из файла, не из fd."""
        with self._lock:
            meta = self._meta
            size = self._size

        view = StreamFileView(self._log_path)

        return view.slice_before(size, window, size, meta)

    def _write_meta(self) -> None:
        """Сайдкар атомарно: rename не оставит битого json при падении."""
        tmp = JournalFile.tmp_of(self._meta_path, os.getpid())

        try:
            with open(tmp, "w", encoding=ByteText.ENCODING) as f:
                json.dump(self._meta.model_dump(), f, ensure_ascii=False)
            os.rename(tmp, self._meta_path)
        except OSError:
            # сайдкар побочен: без него журнал читается как закрытый без итога
            logger.warning("stream meta is not written: %s", self._meta_path)


class _TaskKind(StrEnum):
    """Что writer-поток делает с записью."""

    DATA = "data"
    CLOSE = "close"


@dataclass(frozen=True)
class _WriteTask:
    """Задание writer-потоку: порция байтов либо закрытие пометкой."""

    kind: _TaskKind
    record: JournalRecord
    data: bytes
    note: str


class JournalWriter:
    """Выделенный поток записи журнала вызова: буфер вместо диска в цикле насоса.

    Насос кладёт байты в ограниченный буфер и сразу возвращается к selector'у;
    файлы пишет этот поток. Буфер переполнен — журнал канала сдаётся пометкой,
    поток стадии не рвётся. Сбой на задании гасится здесь же: без этого
    потерялись бы и остальные каналы вызова.
    """

    BUFFER_BYTES: ClassVar[int] = 8 * 1024 * 1024
    JOIN_SEC: ClassVar[float] = 10.0

    def __init__(self, call_id: str, on_write: Callable[[StreamKey], None]) -> None:
        self._on_write = on_write
        self._tasks: list[_WriteTask] = []
        self._buffered = 0
        self._stopped = False
        self._cond = threading.Condition()
        self._thread = threading.Thread(
            target=self._loop,
            name=f"journal-{call_id}",
            daemon=True,
        )
        self._thread.start()

    def submit(self, record: JournalRecord, data: bytes) -> WriteAck:
        """Поставить порцию в очередь; буфер полон — OVERFLOW, ждать нельзя."""
        with self._cond:
            if self._stopped:
                return WriteAck.STOPPED

            if self._buffered + len(data) > self.BUFFER_BYTES:
                return WriteAck.OVERFLOW

            task = _WriteTask(
                kind=_TaskKind.DATA, record=record, data=data, note=""
            )
            self._tasks.append(task)
            self._buffered += len(data)
            self._cond.notify()

        return WriteAck.OK

    def finish(self, record: JournalRecord, note: str) -> None:
        """Закрыть файл пометкой после уже поставленных порций."""
        with self._cond:
            if self._stopped:
                return

            task = _WriteTask(
                kind=_TaskKind.CLOSE, record=record, data=b"", note=note
            )
            self._tasks.append(task)
            self._cond.notify()

    def stop(self) -> None:
        """Заданий больше не будет: поток дописывает очередь и выходит."""
        with self._cond:
            if self._stopped:
                return

            self._stopped = True
            self._cond.notify()

        self._thread.join(self.JOIN_SEC)

        if self._thread.is_alive():
            logger.warning("journal writer did not finish in %.1fs", self.JOIN_SEC)

    def _loop(self) -> None:
        while True:
            task = self._take()
            if task is None:
                return

            try:
                self._run(task)
            except Exception:
                logger.exception("journal writer failed on %s", task.record.log_path)

    def _take(self) -> _WriteTask | None:
        with self._cond:
            while not self._tasks:
                if self._stopped:
                    return None

                self._cond.wait()

            task = self._tasks.pop(0)
            self._buffered -= len(task.data)

            return task

    def _run(self, task: _WriteTask) -> None:
        if task.kind is _TaskKind.CLOSE:
            task.record.finish(task.note)
            self._on_write(task.record.key)
            return

        try:
            task.record.write(task.data)
        except OSError as exc:
            logger.warning(
                "journal write failed: %s: %s", task.record.log_path, exc
            )
            task.record.finish(JournalNote.failed(exc))

        self._on_write(task.record.key)


class JournalSink(ChannelSink):
    """Приёмник канала: байты в буфер writer-потока, файл пишет он.

    Сбой журнала (переполнение буфера, остановленный writer) закрывает файл
    пометкой и отключает приёмник — стадия продолжает работать без журнала.
    """

    def __init__(self, record: JournalRecord, writer: JournalWriter) -> None:
        self._record = record
        self._writer = writer
        self._off = False

    @property
    def key(self) -> StreamKey:
        return self._record.key

    def feed(self, data: bytes) -> None:
        if self._off:
            return

        if not data:
            return

        ack = self._writer.submit(self._record, data)
        if ack is WriteAck.OK:
            return

        self._off = True
        logger.warning(
            "journal sink disabled: %s: %s", self._record.log_path, ack.value
        )
        self._writer.finish(self._record, JournalNote.of(ack).value)

    def close(self) -> None:
        if self._off:
            return

        self._off = True
        self._writer.finish(self._record, JournalNote.DONE.value)


class NoJournalSink(ChannelSink):
    """Журнала нет (том полон или файл не открылся): байты уходят в никуда."""

    def feed(self, data: bytes) -> None:
        """Писать некуда: канал качается дальше, стадия ничего не замечает."""

    def close(self) -> None:
        """Закрывать нечего."""


class JournalListener(Protocol):
    """Подписчик реестра живых вызовов.

    on_data приходит из writer-потока журнала, on_open/on_close — из потока
    вызова: подписчик обязан быть готов к чужому потоку.
    """

    @abstractmethod
    def on_open(self, call: CallJournal) -> None:
        """Вызов открыл журнал."""
        ...

    @abstractmethod
    def on_data(self, key: StreamKey) -> None:
        """В файл канала дописана порция либо он закрыт."""
        ...

    @abstractmethod
    def on_close(self, call: CallJournal) -> None:
        """Журнал вызова закрыт целиком."""
        ...


class JournalRegistry:
    """Реестр живых вызовов: защита файлов от вытеснения и подписка потребителей.

    Подписчик, сорвавшийся на событии, снимается с подписки: журнал живёт
    ради файлов, а не ради показа.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._calls: dict[str, CallJournal] = {}
        self._listeners: list[JournalListener] = []

    def subscribe(self, listener: JournalListener) -> None:
        with self._lock:
            if listener in self._listeners:
                return

            self._listeners.append(listener)

    def unsubscribe(self, listener: JournalListener) -> None:
        with self._lock:
            if listener not in self._listeners:
                return

            self._listeners.remove(listener)

    def live(self) -> tuple[CallJournal, ...]:
        with self._lock:
            return tuple(self._calls.values())

    def live_prefixes(self) -> frozenset[str]:
        """Префиксы файлов живых вызовов: вытеснять их нельзя."""
        prefixes: set[str] = set()
        for call in self.live():
            prefixes.add(call.prefix)

        return frozenset(prefixes)

    def live_threads(self) -> frozenset[str]:
        threads: set[str] = set()
        for call in self.live():
            threads.add(call.context.thread_id)

        return frozenset(threads)

    def call_of(self, thread_id: str, call_id: str) -> CallJournal | None:
        """Живой вызов по адресу; None — вызов уже закрыт либо не начинался."""
        with self._lock:
            return self._calls.get(StreamKey.prefix_of(thread_id, call_id))

    def begin(self, call: CallJournal) -> None:
        """Журнал вызова открыт: реестр ведёт журнал, зовёт его CallJournal."""
        with self._lock:
            self._calls[call.prefix] = call

        for listener in self._snapshot():
            self._fire_open(listener, call)

    def finish(self, call: CallJournal) -> None:
        with self._lock:
            self._calls.pop(call.prefix, None)

        for listener in self._snapshot():
            self._fire_close(listener, call)

    def data(self, key: StreamKey) -> None:
        for listener in self._snapshot():
            self._fire_data(listener, key)

    def _snapshot(self) -> tuple[JournalListener, ...]:
        with self._lock:
            return tuple(self._listeners)

    def _fire_open(self, listener: JournalListener, call: CallJournal) -> None:
        try:
            listener.on_open(call)
        except Exception:
            self._drop(listener)

    def _fire_close(self, listener: JournalListener, call: CallJournal) -> None:
        try:
            listener.on_close(call)
        except Exception:
            self._drop(listener)

    def _fire_data(self, listener: JournalListener, key: StreamKey) -> None:
        try:
            listener.on_data(key)
        except Exception:
            self._drop(listener)

    def _drop(self, listener: JournalListener) -> None:
        logger.exception("journal listener failed; dropped from the registry")
        self.unsubscribe(listener)


class CallJournal:
    """Журнал одного вызова: файлы каналов его стадий и общий writer-поток.

    Сколько бы стадий и каналов ни было, диск обслуживает одна нить: насос
    только кладёт байты в её буфер. Закрытие дозакрывает недописанные каналы
    пометкой и дожидается потока — после него файлы читаемы целиком.
    """

    def __init__(self, journal: StreamJournal, context: ToolCallContext) -> None:
        self._journal = journal
        self._context = context
        self._lock = threading.Lock()
        self._records: dict[str, JournalRecord] = {}
        self._closed = False
        self._writer = JournalWriter(context.call_id, journal.registry.data)

    @property
    def context(self) -> ToolCallContext:
        return self._context

    @property
    def prefix(self) -> str:
        return self._context.prefix

    def journals(self) -> tuple[StreamKey, ...]:
        """Адреса открытых журналов вызова: их отдаёт итог исполнения."""
        with self._lock:
            records = tuple(self._records.values())

        keys: list[StreamKey] = []
        for record in records:
            keys.append(record.key)

        return tuple(keys)

    def sink(self, stage: str, channel: Channel) -> ChannelSink:
        """Приёмник журнала канала стадии; том не принял — байты в никуда."""
        key = self._context.key(stage, channel)

        with self._lock:
            if self._closed:
                raise JournalError(f"call journal is closed: {key.rel_log()}")

        try:
            record = self._journal.open_record(key)
        except JournalError as exc:
            logger.warning("journal is unavailable for %s: %s", key.rel_log(), exc)
            return NoJournalSink()

        with self._lock:
            self._records[key.rel_log()] = record

        return JournalSink(record, self._writer)

    def record_of(self, key: StreamKey) -> JournalRecord | None:
        """Открытый рекорд канала; None — такого канала у вызова нет."""
        with self._lock:
            return self._records.get(key.rel_log())

    def close(self, note: str) -> None:
        """Дозакрыть каналы пометкой, остановить поток, сняться с реестра."""
        with self._lock:
            if self._closed:
                return

            self._closed = True
            records = tuple(self._records.values())

        for record in records:
            if record.closed:
                continue

            self._writer.finish(record, note)

        self._writer.stop()
        self._journal.registry.finish(self)


class StreamJournal:
    """Журналы вызовов в служебном томе: запись каналов, чтение окнами, реестр.

    Перед открытием журнала вызова держится резерв места: старейшие вызовы
    вытесняются целиком, пока резерв не появится; живые вызовы (реестр)
    ротация не трогает. Общий потолок держит точка монтирования под корнем
    журналов: её размер и есть квота.
    """

    WINDOW_BYTES: ClassVar[int] = 64 * 1024

    def __init__(self, vault: DirVault, reserve_bytes: int) -> None:
        if reserve_bytes < 0:
            msg = f"reserve_bytes must be >= 0, got {reserve_bytes}"
            raise ValueError(msg)

        self._vault = vault
        self._reserve = reserve_bytes
        self._registry = JournalRegistry()

    @property
    def registry(self) -> JournalRegistry:
        return self._registry

    def open(self, context: ToolCallContext) -> CallJournal:
        """Открыть журнал вызова: резерв тома, writer-поток, запись в реестр."""
        root = self._vault.root_for(context.user_id)

        self._ensure_reserve(context.user_id, self._protected(context.prefix))

        call = CallJournal(self, context)
        self._registry.begin(call)

        logger.info(
            "tool journal: %s -> %s", context.tool, os.path.join(root, context.prefix)
        )

        return call

    def open_record(self, key: StreamKey) -> JournalRecord:
        """Файл канала на запись; ENOSPC вытесняет старые вызовы до успеха.

        Каталог треда создаётся в цикле: вытеснение могло снести опустевший
        каталог того же треда, куда пишем.
        """
        root = self._vault.root_for(key.user_id)
        protected = self._protected(key.call_prefix)

        log_path = os.path.join(root, key.rel_log())
        meta_path = os.path.join(root, key.rel_meta())

        while True:
            try:
                os.makedirs(os.path.dirname(log_path), exist_ok=True)
                return JournalRecord(key, log_path, meta_path)
            except OSError as exc:
                if exc.errno != errno.ENOSPC:
                    raise JournalError(
                        f"stream log is not writable: {log_path}: {exc}"
                    ) from exc

                freed = self._evict_oldest(key.user_id, root, protected)
                if freed < 0:
                    raise JournalError(
                        f"stream vault is full, nothing to evict: {log_path}"
                    ) from exc

                logger.info(
                    "journal evicted %d bytes to open %s", freed, key.rel_log()
                )

    def keys_of(
        self, user_id: str, thread_id: str, call_id: str
    ) -> tuple[StreamKey, ...]:
        """Адреса записанных каналов вызова, последний записанный — последним.

        Порядок по времени записи: у графа продукт последней стадии
        дописывается позже остальных, и панель открывает именно его.
        """
        root = self._vault.root_for(user_id)
        path = os.path.join(root, thread_id)

        written: dict[str, float] = {}
        found: dict[str, StreamKey] = {}

        for key, stat in self._journal_files(user_id, thread_id, path):
            if key.call_id != call_id:
                continue

            rel = key.rel_log()
            known = written.get(rel, 0.0)
            written[rel] = max(known, stat.st_mtime)
            found[rel] = key

        order = sorted(found, key=lambda rel: written[rel])

        keys: list[StreamKey] = []
        for rel in order:
            keys.append(found[rel])

        return tuple(keys)

    def usage(self, user_id: str) -> VaultUsage:
        """Занятость тома: место и журналы по тредам, старые первыми."""
        root = self._vault.root_for(user_id)

        try:
            space = os.statvfs(root)
        except OSError as exc:
            raise JournalError(f"stream vault is not readable: {root}: {exc}") from exc

        threads = sorted(
            self._thread_usages(user_id, root), key=lambda item: item.last_write_at
        )

        return VaultUsage(
            total_bytes=space.f_blocks * space.f_frsize,
            free_bytes=space.f_bavail * space.f_frsize,
            threads=tuple(threads),
        )

    def purge_thread(self, user_id: str, thread_id: str) -> int:
        """Удалить журналы треда; возвращает освобождённые байты."""
        root = self._vault.root_for(user_id)
        path = os.path.join(root, thread_id)

        freed = 0
        for entry in self._thread_usages(user_id, root):
            if entry.thread_id == thread_id:
                freed = entry.bytes_used

        if freed == 0 and not os.path.isdir(path):
            return 0

        try:
            shutil.rmtree(path)
        except OSError as exc:
            raise JournalError(f"journal purge failed: {path}: {exc}") from exc

        return freed

    def slice_at(self, key: StreamKey, offset: int) -> StreamSlice | None:
        """Окно журнала вперёд; None — файла такого канала нет.

        offset меньше нуля — хвост файла.
        """
        opened = self._open_view(key)
        if opened is None:
            return None

        view, size, meta = opened

        try:
            if offset < 0:
                return view.slice_before(size, self.WINDOW_BYTES, size, meta)

            return view.slice_at(offset, self.WINDOW_BYTES, size, meta)
        except OSError as exc:
            raise JournalError(
                f"stream log is not readable: {key.rel_log()}: {exc}"
            ) from exc

    def slice_before(self, key: StreamKey, end: int) -> StreamSlice | None:
        """Окно, заканчивающееся на end: прокрутка вверх стыкуется встык."""
        opened = self._open_view(key)
        if opened is None:
            return None

        view, size, meta = opened

        try:
            return view.slice_before(end, self.WINDOW_BYTES, size, meta)
        except OSError as exc:
            raise JournalError(
                f"stream log is not readable: {key.rel_log()}: {exc}"
            ) from exc

    def head(self, key: StreamKey, max_bytes: int) -> StreamSlice | None:
        """Голова файла до max_bytes: столько канала уезжает в модель."""
        if max_bytes <= 0:
            raise ValueError(f"max_bytes must be positive, got {max_bytes}")

        opened = self._open_view(key)
        if opened is None:
            return None

        view, size, meta = opened

        try:
            return view.slice_at(0, max_bytes, size, meta)
        except OSError as exc:
            raise JournalError(
                f"stream log is not readable: {key.rel_log()}: {exc}"
            ) from exc

    def vault_root(self, user_id: str) -> str:
        """Корень тома пользователя: под ним лежат файлы каналов по тредам.

        Отдаётся скачиванию журнала — файл течёт из тома тем же путём, что и
        вложения.
        """
        return self._vault.root_for(user_id)

    def _protected(self, own_prefix: str) -> frozenset[str]:
        """Живые вызовы плюс свой: их файлы вытеснению не подлежат."""
        return self._registry.live_prefixes() | {own_prefix}

    def _ensure_reserve(self, user_id: str, protected: frozenset[str]) -> None:
        """LRU-вытеснение вызовов до резерва; нечего вытеснять — предупредить."""
        if not self._reserve:
            return

        root = self._vault.root_for(user_id)

        while self._free_bytes(root) < self._reserve:
            freed = self._evict_oldest(user_id, root, protected)
            if freed < 0:
                logger.warning(
                    "journal reserve is not met: %d < %d",
                    self._free_bytes(root),
                    self._reserve,
                )
                return

    def _evict_oldest(
        self, user_id: str, root: str, protected: frozenset[str]
    ) -> int:
        """Удалить старейший незащищённый вызов; -1 — вытеснять нечего."""
        victim = min(
            self._evictable(user_id, root, protected),
            default=None,
            key=lambda entry: entry.last_write_at,
        )
        if victim is None:
            return -1

        self._remove_call(root, victim)

        logger.info(
            "journal evicted call %s of thread %s (%d bytes)",
            victim.call_id,
            victim.thread_id,
            victim.bytes_used,
        )

        return victim.bytes_used

    def _evictable(
        self, user_id: str, root: str, protected: frozenset[str]
    ) -> Iterator[CallLogUsage]:
        for entry in self._call_logs(user_id, root):
            if entry.prefix in protected:
                continue

            yield entry

    @staticmethod
    def _remove_call(root: str, entry: CallLogUsage) -> None:
        """Снести все файлы вызова; опустевший каталог треда убрать."""
        for rel in entry.files:
            try:
                os.remove(os.path.join(root, rel))
            except FileNotFoundError:
                continue
            except OSError:
                logger.warning("journal eviction failed on %s", rel, exc_info=True)

        try:
            os.rmdir(os.path.join(root, entry.thread_id))
        except OSError:
            return

    def _call_logs(self, user_id: str, root: str) -> Iterator[CallLogUsage]:
        for thread_entry in self._thread_dirs(root):
            yield from self._thread_calls(user_id, thread_entry.name)

    def _thread_calls(self, user_id: str, thread_id: str) -> Iterator[CallLogUsage]:
        """Файлы треда, сгруппированные по вызову: вызов — единица учёта."""
        root = self._vault.root_for(user_id)
        path = os.path.join(root, thread_id)

        totals: dict[str, _CallTotals] = {}

        for key, stat in self._journal_files(user_id, thread_id, path):
            entry = totals.get(key.call_id)
            if entry is None:
                entry = _CallTotals(bytes_used=0, last_write_at=0.0, files=[])
                totals[key.call_id] = entry

            entry.bytes_used += stat.st_size
            entry.last_write_at = max(entry.last_write_at, stat.st_mtime)
            entry.files.append(key.rel_log())
            entry.files.append(key.rel_meta())

        for call_id, entry in totals.items():
            yield CallLogUsage(
                thread_id=thread_id,
                call_id=call_id,
                bytes_used=entry.bytes_used,
                last_write_at=entry.last_write_at,
                files=tuple(dict.fromkeys(entry.files)),
            )

    def _thread_usages(self, user_id: str, root: str) -> Iterator[ThreadUsage]:
        for thread_entry in self._thread_dirs(root):
            calls = tuple(self._thread_calls(user_id, thread_entry.name))

            used = 0
            last_write = 0.0
            for call in calls:
                used += call.bytes_used
                last_write = max(last_write, call.last_write_at)

            yield ThreadUsage(
                thread_id=thread_entry.name,
                bytes_used=used,
                calls=len(calls),
                last_write_at=last_write,
            )

    @staticmethod
    def _thread_dirs(root: str) -> Iterator[os.DirEntry[str]]:
        try:
            entries = list(os.scandir(root))
        except OSError as exc:
            raise JournalError(f"stream vault is not readable: {root}: {exc}") from exc

        for entry in entries:
            if not entry.is_dir(follow_symlinks=False):
                continue

            yield entry

    @staticmethod
    def _journal_files(
        user_id: str, thread_id: str, path: str
    ) -> Iterator[tuple[StreamKey, os.stat_result]]:
        """Файлы журнала треда с их адресами; посторонние имена пропускаются."""
        try:
            entries = list(os.scandir(path))
        except FileNotFoundError:
            return
        except OSError as exc:
            raise JournalError(f"thread dir is not readable: {path}: {exc}") from exc

        for entry in entries:
            if not entry.is_file(follow_symlinks=False):
                continue

            try:
                key = StreamKey.of_file(user_id, thread_id, entry.name)
            except ChannelError:
                logger.debug("journal: foreign file in the vault: %s", entry.path)
                continue

            yield key, entry.stat()

    @staticmethod
    def _free_bytes(root: str) -> int:
        space = os.statvfs(root)

        return space.f_bavail * space.f_frsize

    def _open_view(
        self, key: StreamKey
    ) -> tuple[StreamFileView, int, StreamMeta] | None:
        root = self._vault.root_for(key.user_id)
        log_path = os.path.join(root, key.rel_log())

        try:
            size = os.stat(log_path).st_size
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise JournalError(
                f"stream log is not readable: {key.rel_log()}: {exc}"
            ) from exc

        meta = self._read_meta(os.path.join(root, key.rel_meta()))

        return StreamFileView(log_path), size, meta

    @staticmethod
    def _read_meta(meta_path: str) -> StreamMeta:
        """Сайдкар потерян или бит — журнал считается закрытым без итога."""
        try:
            with open(meta_path, encoding=ByteText.ENCODING) as f:
                raw = json.load(f)
            return StreamMeta.model_validate(raw)
        except (OSError, ValueError):
            return StreamMeta(closed=True, note="")


class StreamJournalHub:
    """Журнал приложения: одна точка доступа для UI, тулов и data layer.

    Журнал обязателен: без настроенного тома приложение не работает.
    """

    _JOURNAL: ClassVar[StreamJournal | None] = None

    @classmethod
    def configure(cls, journal: StreamJournal) -> None:
        cls._JOURNAL = journal

    @classmethod
    def get(cls) -> StreamJournal:
        """Журнал приложения; не настроен — JournalError."""
        journal = cls._JOURNAL
        if journal is None:
            raise JournalError("stream journal is not configured")

        return journal

    @classmethod
    def reset(cls) -> None:
        """Сброс: пользуются тесты, приложению это не нужно."""
        cls._JOURNAL = None


class StageJournalSinks:
    """Журнальные приёмники исходящих каналов одной стадии.

    Единственное место, где решается, что журналируется: все исходящие каналы
    стадии без исключений, включая tool_result и wrap-каналы mount-группы.
    """

    @classmethod
    def of(
        cls,
        call: CallJournal,
        stage: str,
        channels: Sequence[Channel],
    ) -> dict[Channel, ChannelSink]:
        sinks: dict[Channel, ChannelSink] = {}

        for channel in channels:
            if channel.writes_in:
                continue

            sinks[channel] = call.sink(stage, channel)

        return sinks
