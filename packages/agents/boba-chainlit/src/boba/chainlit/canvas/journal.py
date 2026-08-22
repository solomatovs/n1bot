"""Журнал вывода инструментов: модели, порты и файловая реализация.

Файл на канал вызова в томе пользователя: {thread}/{call_id}.{tool}.{channel}.log
плюс сайдкар {call_id}.meta.json с итогом; чтение — окнами по смещению.
Единица учёта и вытеснения — вызов целиком: все его файлы вместе.
Модуль не знает про chainlit: им пользуются слой данных и панель.

Ошибки: StreamJournalError — том недоступен, файл не открывается или место
кончилось; сбои записи гасятся внутрь, закрывая поток пометкой.
"""

from __future__ import annotations

import errno
import json
import logging
import os
import re
import shutil
import threading
from collections.abc import Callable, Iterator
from enum import IntEnum, StrEnum
from typing import ClassVar, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator

from boba.toolkit.channels import JournalChannel, JournalChannels
from boba.toolkit.stream import Chunk, StreamSink

__all__ = [
    "CallLogUsage",
    "DirVault",
    "JournalFile",
    "JournalText",
    "JournalWindow",
    "LogName",
    "PathSegment",
    "StreamFileView",
    "StreamJournal",
    "StreamJournalError",
    "StreamJournalHub",
    "StreamKey",
    "StreamMeta",
    "StreamRecorder",
    "StreamRecorderPort",
    "StreamSlice",
    "StreamStat",
    "StreamStorePort",
    "ThreadUsage",
    "VaultPath",
    "VaultUsage",
    "WindowAlign",
]

logger = logging.getLogger(__name__)


class StreamJournalError(Exception):
    """Том журнала недоступен: писать некуда."""


class LogName(BaseModel):
    """Разобранное имя файла журнала: вызов, инструмент, канал.

    Имя чужого формата не отвергается: весь стем считается call_id, чтобы
    учёт места видел и мог вытеснить любой файл тома.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    SEGMENTS: ClassVar[int] = 3
    """Сегментов в стеме: call_id, tool, channel."""

    call_id: str
    tool: str = ""
    channel: str = ""

    @classmethod
    def parse(cls, stem: str) -> LogName:
        """Разбор по сегментам с конца."""
        segments = stem.split(".")
        if len(segments) < cls.SEGMENTS:
            return cls(call_id=stem)

        return cls(
            call_id=segments[0],
            tool=".".join(segments[1:-1]),
            channel=segments[-1],
        )


class JournalFile(StrEnum):
    """Суффиксы файлов журнала; сборка и разбор путей — только здесь.

    Лог вызова: {thread}/{call_id}.{tool}.{channel}.log — файл на канал.
    Сайдкар с итогом один на вызов: {thread}/{call_id}.meta.json. Разбор
    имени идёт по сегментам с конца; срезом суффикса call_id не берётся.
    """

    LOG = ".log"
    META = ".meta.json"
    TMP = ".tmp"

    @classmethod
    def rel_log(
        cls, thread_id: str, call_id: str, tool: str, channel: JournalChannel
    ) -> str:
        if "." in call_id or "." in tool:
            msg = f"log name segments must not contain dots: {call_id!r}, {tool!r}"
            raise ValueError(msg)

        return f"{thread_id}/{call_id}.{tool}.{channel.value}{cls.LOG}"

    @classmethod
    def rel_meta(cls, thread_id: str, call_id: str) -> str:
        return f"{thread_id}/{call_id}{cls.META}"

    @classmethod
    def call_prefix(cls, thread_id: str, call_id: str) -> str:
        """Префикс всех файлов вызова: единица защиты и вытеснения."""
        return f"{thread_id}/{call_id}."

    @classmethod
    def is_log(cls, name: str) -> bool:
        return name.endswith(cls.LOG)

    @classmethod
    def is_meta(cls, name: str) -> bool:
        return name.endswith(cls.META)

    @classmethod
    def parse_log(cls, log_name: str) -> LogName:
        """Имя лога: стем без суффикса разбирает LogName по сегментам."""
        return LogName.parse(log_name[: -len(cls.LOG)])

    @classmethod
    def call_id_of_meta(cls, meta_name: str) -> str:
        return meta_name[: -len(cls.META)]

    @classmethod
    def tmp_of(cls, path: str) -> str:
        return f"{path}{cls.TMP}.{os.getpid()}"


class JournalText(StrEnum):
    """Текстовый кодек журнала: utf-8, битые байты замещаются при чтении."""

    ENCODING = "utf-8"
    DECODE_ERRORS = "replace"

    @classmethod
    def encode(cls, text: str) -> bytes:
        return text.encode(cls.ENCODING)

    @classmethod
    def decode(cls, data: bytes) -> str:
        return data.decode(cls.ENCODING, errors=cls.DECODE_ERRORS)


class PathSegment:
    """Проверка сегмента пути в томе журнала: одна на ключи и на сам том.

    Сегмент приходит извне — идентификатор пользователя из аутентификации,
    thread_id из ссылки, call_id из протокола провайдера, — а дальше уходит
    в os.path.join, поэтому проверяется на входе и только здесь.
    """

    SAFE: ClassVar[re.Pattern[str]] = re.compile(r"[A-Za-z0-9_-][A-Za-z0-9._-]*")
    """Сегмент целиком: только безопасные символы и без точки в начале."""

    @classmethod
    def checked(cls, value: str) -> str:
        if not cls.SAFE.fullmatch(value):
            msg = f"unsafe path segment: {value!r}"
            raise ValueError(msg)

        return value


class VaultPath:
    """Путь внутри тома: собирается, канонизируется и проверяется на выход наружу.

    Проверка сегментов (PathSegment) отсекает `..` и слэш в идентификаторе, а
    здесь ловится то, чего по строке не видно: симлинк внутри тома, ведущий
    за его пределы.
    """

    @classmethod
    def inside(cls, root: str, *parts: str) -> str:
        base = os.path.realpath(root)
        candidate = os.path.realpath(os.path.join(base, *parts))

        if candidate == base:
            return candidate

        if candidate.startswith(base + os.sep):
            return candidate

        msg = f"path escapes the vault: {candidate!r}"
        raise StreamJournalError(msg)


class StreamKey(BaseModel):
    """Адрес журнала одного вызова: {thread_id}/{call_id} в томе пользователя.

    call_id приходит из протокола LLM-провайдера — в путь допускаются только
    безопасные символы, всё прочее отвергается на границе. Точка в call_id
    запрещена: имя файла {call_id}.{tool}.{channel}.log разбирается по
    сегментам, и точка внутри сделала бы его неразложимым.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    user_id: str = Field(min_length=1)
    thread_id: str = Field(min_length=1)
    call_id: str = Field(min_length=1, max_length=255)

    @field_validator("user_id", "thread_id", "call_id")
    @classmethod
    def _safe_segment(cls, value: str) -> str:
        return PathSegment.checked(value)

    @field_validator("call_id")
    @classmethod
    def _no_dots(cls, value: str) -> str:
        if "." in value:
            msg = f"call_id must not contain dots: {value!r}"
            raise ValueError(msg)

        return value

    def rel_log(self, tool: str, channel: JournalChannel) -> str:
        return JournalFile.rel_log(self.thread_id, self.call_id, tool, channel)

    def rel_meta(self) -> str:
        return JournalFile.rel_meta(self.thread_id, self.call_id)

    def call_prefix(self) -> str:
        return JournalFile.call_prefix(self.thread_id, self.call_id)


class StreamMeta(BaseModel):
    """Сайдкар журнала: имя инструмента и итог записи."""

    model_config = ConfigDict(extra="ignore")

    tool_name: str
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


class StreamStat(BaseModel):
    """Состояние файла потока без чтения тела: размер и итог записи."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    size: int
    closed: bool
    note: str


class WindowAlign:
    """Выравнивание окна чтения по границам строк: чистая логика без I/O.

    Окна стыкуются встык без рваных строк: голова окна сдвигается за первый
    перевод строки (кроме начала файла и стыка сразу за переводом строки),
    хвост forward-окна обрезается по последнему переводу (кроме конца файла).
    Строка длиннее окна отдаётся как есть — прогресс важнее красоты.
    """

    @staticmethod
    def head(start: int, raw: bytes) -> tuple[int, bytes]:
        """Голова окна по границе строки.

        raw прочитан с start-1 (лишний байт впереди), кроме start == 0.
        Стык сразу за переводом строки не трогается; произвольное смещение
        сдвигается за первый перевод строки внутри окна; без переводов вовсе
        (одна строка длиннее окна) — как есть.
        """
        if start == 0:
            return start, raw

        data = raw[1:]
        if raw[:1] == b"\n":
            return start, data

        cut = data.find(b"\n")
        if cut < 0 or cut == len(data) - 1:
            return start, data

        return start + cut + 1, data[cut + 1 :]

    @staticmethod
    def read_plan(start: int, length: int) -> tuple[int, int]:
        """Что читать из файла, чтобы head() смог выровнять голову."""
        if start == 0:
            return 0, length

        return start - 1, length + 1

    @staticmethod
    def forward_trim(start: int, data: bytes, size: int) -> tuple[bytes, int]:
        """Хвост forward-окна по последнему переводу строки (кроме конца файла)."""
        end = start + len(data)
        if end >= size:
            return data, end

        cut = data.rfind(b"\n")
        if 0 <= cut < len(data) - 1:
            data = data[: cut + 1]
            end = start + len(data)

        return data, end


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


class CallLogUsage(BaseModel):
    """Файлы одного вызова: единица учёта и вытеснения при нехватке места.

    Вызов пишет несколько файлов — лог на канал плюс сайдкар; вытесняются
    они только вместе, иначе LRU оставил бы вызов без части каналов.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    thread_id: str
    call_id: str
    rel_files: tuple[str, ...]
    bytes_used: int
    last_write_at: float

    @property
    def prefix(self) -> str:
        return JournalFile.call_prefix(self.thread_id, self.call_id)


class JournalWindow(IntEnum):
    """Размер окна чтения журнала: панель забирает файл кусками, не целиком."""

    BYTES = 64 * 1024


class StreamRecorderPort(StreamSink, Protocol):
    """Писатель журнала одного вызова: слежению нужны размер, итог и закрытие."""

    @property
    def closed(self) -> bool: ...

    @property
    def size(self) -> int: ...

    @property
    def note(self) -> str: ...

    def close(self, note: str) -> None: ...


class StreamStorePort(Protocol):
    """Журнал приложения: открыть писателя канала и прочитать окно."""

    def recorder(
        self,
        key: StreamKey,
        tool_name: str,
        channel: JournalChannel,
        on_data: Callable[[], None],
        protected_prefixes: frozenset[str],
    ) -> StreamRecorderPort: ...

    def slice_at(
        self, key: StreamKey, offset: int, channel: JournalChannel
    ) -> StreamSlice | None: ...

    def slice_before(
        self, key: StreamKey, end: int, channel: JournalChannel
    ) -> StreamSlice | None: ...

    def stat_of(self, key: StreamKey, channel: JournalChannel) -> StreamStat | None: ...

    def log_rel_path(self, key: StreamKey, channel: JournalChannel) -> str | None: ...

    def channels_of(self, key: StreamKey) -> tuple[JournalChannel, ...]: ...

    def usage(self, user_id: str) -> VaultUsage: ...

    def purge_thread(self, user_id: str, thread_id: str) -> int: ...

    def vault_root(self, user_id: str) -> str: ...


class StreamJournalHub:
    """Журнал приложения: одна точка доступа для панели, тулов и слоя данных."""

    _JOURNAL: ClassVar[StreamStorePort | None] = None

    @classmethod
    def configure(cls, journal: StreamStorePort) -> None:
        cls._JOURNAL = journal

    @classmethod
    def get(cls) -> StreamStorePort | None:
        return cls._JOURNAL

    @classmethod
    def reset(cls) -> None:
        """Сброс: пользуются тесты, приложению это не нужно."""
        cls._JOURNAL = None


class DirVault:
    """Том-каталог: без монтирований и привилегий, квоту держит журнал."""

    def __init__(self, root: str) -> None:
        self._root = root

    def root_for(self, user_id: str) -> str:
        """Каталог тома; сегмент сверяется с шаблоном прямо перед сборкой пути."""
        if not PathSegment.SAFE.fullmatch(user_id):
            msg = f"unsafe vault segment: {user_id!r}"
            raise StreamJournalError(msg)

        path = VaultPath.inside(self._root, user_id)
        os.makedirs(path, exist_ok=True)
        return path


class StreamRecorder(StreamRecorderPort):
    """Писатель журнала одного вызова: append по мере работы инструмента.

    Реализует StreamSink: сбой записи (кончилось место, недоступен том) не
    выходит наружу — журнал закрывается пометкой, инструмент работает дальше.
    on_data зовётся после каждой порции и при закрытии.
    """

    FILE_MODE: ClassVar[int] = 0o600

    def __init__(
        self,
        log_path: str,
        meta_path: str,
        tool_name: str,
        on_data: Callable[[], None],
    ) -> None:
        self._log_path = log_path
        self._meta_path = meta_path
        self._on_data = on_data
        self._lock = threading.Lock()
        self._closed = False
        self._meta = StreamMeta(tool_name=tool_name)

        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
        descriptor = os.open(log_path, flags, self.FILE_MODE)
        self._file = os.fdopen(descriptor, "wb")
        self._size = os.fstat(self._file.fileno()).st_size
        self._write_meta()

    @property
    def log_path(self) -> str:
        return self._log_path

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def size(self) -> int:
        with self._lock:
            return self._size

    @property
    def note(self) -> str:
        with self._lock:
            return self._meta.note

    def feed(self, data: Chunk) -> None:
        if not data:
            return

        note = ""
        with self._lock:
            if self._closed:
                return

            note = self._append(data)

        if note:
            self.close(note)
            return

        self._on_data()

    def _append(self, data: Chunk) -> str:
        """Дописать порцию под локом; непустая строка — причина закрытия."""
        try:
            # панель читает файл по смещению по ходу работы: буфер держать нельзя
            self._file.write(data)
            self._file.flush()
            self._size += len(data)
        except OSError as exc:
            logger.warning("stream journal write failed: %s: %s", self._log_path, exc)
            return f"journal stopped: {exc.strerror or exc}"

        return ""

    def feed_text(self, text: str) -> None:
        self.feed(JournalText.encode(text))

    def close(self, note: str) -> None:
        """Идемпотентное закрытие: первый вызов фиксирует итог в сайдкаре."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._meta = self._meta.model_copy(update={"closed": True, "note": note})
            self._file.close()

        self._write_meta()
        self._on_data()

    def _write_meta(self) -> None:
        """Сайдкар атомарно: rename не оставит битого json при падении."""
        tmp = JournalFile.tmp_of(self._meta_path)

        try:
            with open(tmp, "w", encoding=JournalText.ENCODING) as f:
                json.dump(self._meta.model_dump(), f, ensure_ascii=False)
            os.rename(tmp, self._meta_path)
        except OSError:
            logger.warning("stream meta is not written: %s", self._meta_path)


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

        data, end = WindowAlign.forward_trim(start, data, size)

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
        """Чтение с головой на границе строки; выравнивание — WindowAlign."""
        if length <= 0:
            return start, b""

        read_start, read_length = WindowAlign.read_plan(start, length)
        raw = self._read(read_start, read_length)

        return WindowAlign.head(start, raw)

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
            text=JournalText.decode(data),
            offset=start,
            end=end,
            size=size,
            window=window,
            closed=meta.closed,
            note=meta.note,
        )


class StreamJournal(StreamStorePort):
    """Журналы вызовов в служебном томе: запись рекордером, чтение окнами.

    Перед открытием нового журнала держится резерв места: старейшие по
    записи треды вытесняются, пока резерв не появится; защищённые треды
    (текущий и с живыми потоками) ротация не трогает. Общий потолок держит
    точка монтирования под корнем журналов: её размер и есть квота.
    """

    def __init__(self, vault: DirVault, reserve_bytes: int) -> None:
        if reserve_bytes < 0:
            msg = f"reserve_bytes must be >= 0, got {reserve_bytes}"
            raise ValueError(msg)

        self._vault = vault
        self._reserve = reserve_bytes

    def recorder(
        self,
        key: StreamKey,
        tool_name: str,
        channel: JournalChannel,
        on_data: Callable[[], None],
        protected_prefixes: frozenset[str],
    ) -> StreamRecorder:
        """Открыть журнал канала на запись; каталог треда создаётся здесь.

        protected_prefixes — префиксы {thread}/{call_id}. живых вызовов,
        вытеснять их файлы нельзя; открываемый вызов защищён всегда. Место
        кончилось — старейшие закрытые вызовы вытесняются до успеха.
        """
        root = self._vault.root_for(key.user_id)
        protected = protected_prefixes | {key.call_prefix()}

        self._ensure_reserve(root, protected)

        log_path = VaultPath.inside(root, key.rel_log(tool_name, channel))

        # вместо релея stderr в общий журнал — одна строка о месте записи
        logger.info("tool stream journal: %s -> %s", tool_name, log_path)

        return self._open_recorder(key, root, tool_name, channel, on_data, protected)

    def _open_recorder(  # noqa: PLR0913
        self,
        key: StreamKey,
        root: str,
        tool_name: str,
        channel: JournalChannel,
        on_data: Callable[[], None],
        protected: frozenset[str],
    ) -> StreamRecorder:
        """Открыть рекордер, вытесняя старые вызовы при ENOSPC до успеха.

        Каталог треда создаётся в цикле: вытеснение могло снести опустевший
        каталог того же треда, куда пишем.
        """
        log_path = VaultPath.inside(root, key.rel_log(tool_name, channel))
        meta_path = VaultPath.inside(root, key.rel_meta())

        while True:
            try:
                os.makedirs(os.path.dirname(log_path), exist_ok=True)
                return StreamRecorder(log_path, meta_path, tool_name, on_data)
            except OSError as exc:
                if exc.errno != errno.ENOSPC:
                    raise StreamJournalError(
                        f"stream log is not writable: {log_path}: {exc}"
                    ) from exc

                freed = self._evict_oldest(root, protected)
                if freed < 0:
                    raise StreamJournalError(
                        f"stream vault is full, nothing to evict: {log_path}: {exc}"
                    ) from exc

                logger.info(
                    "stream journal evicted %d bytes to open %s",
                    freed,
                    key.call_id,
                )

    def usage(self, user_id: str) -> VaultUsage:
        """Занятость тома: место и журналы по тредам, старые первыми."""
        root = self._vault.root_for(user_id)
        space = os.statvfs(root)

        threads = sorted(self._thread_usages(root), key=lambda t: t.last_write_at)

        return VaultUsage(
            total_bytes=space.f_blocks * space.f_frsize,
            free_bytes=space.f_bavail * space.f_frsize,
            threads=tuple(threads),
        )

    def purge_thread(self, user_id: str, thread_id: str) -> int:
        """Удалить журналы треда; возвращает освобождённые байты."""
        root = self._vault.root_for(user_id)

        try:
            segment = PathSegment.checked(thread_id)
        except ValueError as exc:
            raise StreamJournalError(f"unsafe thread segment: {thread_id!r}") from exc

        path = VaultPath.inside(root, segment)

        freed = 0
        for entry in self._thread_usages(root):
            if entry.thread_id == thread_id:
                freed = entry.bytes_used

        if freed == 0 and not os.path.isdir(path):
            return 0

        try:
            shutil.rmtree(path)
        except OSError as exc:
            raise StreamJournalError(
                f"stream journal purge failed: {path}: {exc}"
            ) from exc

        return freed

    def _ensure_reserve(self, root: str, protected: frozenset[str]) -> None:
        """LRU-вытеснение логов до резерва; нечего вытеснять — предупредить."""
        if not self._reserve:
            return

        while self._free_bytes(root) < self._reserve:
            freed = self._evict_oldest(root, protected)
            if freed < 0:
                logger.warning(
                    "stream journal reserve is not met: %d < %d",
                    self._free_bytes(root),
                    self._reserve,
                )
                return

    def _evict_oldest(self, root: str, protected: frozenset[str]) -> int:
        """Удалить старейший незащищённый лог; -1 — вытеснять нечего."""
        victim = min(
            self._evictable(root, protected),
            default=None,
            key=lambda entry: entry.last_write_at,
        )
        if victim is None:
            return -1

        self._remove_call(root, victim)

        logger.info(
            "stream journal evicted call %s of thread %s (%d bytes)",
            victim.call_id,
            victim.thread_id,
            victim.bytes_used,
        )
        return victim.bytes_used

    def _evictable(
        self, root: str, protected: frozenset[str]
    ) -> Iterator[CallLogUsage]:
        for entry in self._call_logs(root):
            if entry.prefix in protected:
                continue

            yield entry

    def _remove_call(self, root: str, entry: CallLogUsage) -> None:
        """Снести все файлы вызова; опустевший каталог треда убрать."""
        for rel in entry.rel_files:
            try:
                os.remove(VaultPath.inside(root, rel))
            except FileNotFoundError:
                continue
            except OSError:
                logger.warning(
                    "stream journal eviction failed on %s", rel, exc_info=True
                )

        try:
            os.rmdir(VaultPath.inside(root, entry.thread_id))
        except OSError:
            return

    @staticmethod
    def _call_files(thread_path: str) -> Iterator[tuple[str, str, os.stat_result]]:
        """Файлы каталога треда: (call_id, имя, stat); чужие имена — мимо."""
        for file_entry in os.scandir(thread_path):
            if not file_entry.is_file(follow_symlinks=False):
                continue

            name = file_entry.name
            if JournalFile.is_log(name):
                yield JournalFile.parse_log(name).call_id, name, file_entry.stat()
                continue

            if JournalFile.is_meta(name):
                yield JournalFile.call_id_of_meta(name), name, file_entry.stat()

    @classmethod
    def _call_logs(cls, root: str) -> Iterator[CallLogUsage]:
        """Файлы каждого вызова одной записью: единица учёта — вызов."""
        for thread_entry in os.scandir(root):
            if not thread_entry.is_dir(follow_symlinks=False):
                continue

            grouped: dict[str, list[tuple[str, os.stat_result]]] = {}
            for call_id, name, stat in cls._call_files(thread_entry.path):
                grouped.setdefault(call_id, []).append((name, stat))

            for call_id, files in grouped.items():
                rel_files: list[str] = []
                used = 0
                last_write = 0.0
                for name, stat in files:
                    rel_files.append(f"{thread_entry.name}/{name}")
                    used += stat.st_size
                    last_write = max(last_write, stat.st_mtime)

                yield CallLogUsage(
                    thread_id=thread_entry.name,
                    call_id=call_id,
                    rel_files=tuple(sorted(rel_files)),
                    bytes_used=used,
                    last_write_at=last_write,
                )

    @staticmethod
    def _free_bytes(root: str) -> int:
        space = os.statvfs(root)
        return space.f_bavail * space.f_frsize

    @staticmethod
    def _thread_usages(root: str) -> Iterator[ThreadUsage]:
        for thread_entry in os.scandir(root):
            if not thread_entry.is_dir(follow_symlinks=False):
                continue

            used = 0
            call_ids: set[str] = set()
            last_write = 0.0
            for file_entry in os.scandir(thread_entry.path):
                if not file_entry.is_file(follow_symlinks=False):
                    continue
                stat = file_entry.stat()
                used += stat.st_size
                last_write = max(last_write, stat.st_mtime)
                if JournalFile.is_log(file_entry.name):
                    call_ids.add(JournalFile.parse_log(file_entry.name).call_id)
            calls = len(call_ids)

            yield ThreadUsage(
                thread_id=thread_entry.name,
                bytes_used=used,
                calls=calls,
                last_write_at=last_write,
            )

    def slice_at(
        self, key: StreamKey, offset: int, channel: JournalChannel
    ) -> StreamSlice | None:
        """Окно записанного журнала вперёд; None — журнала такого вызова нет.

        offset меньше нуля — хвост файла.
        """
        opened = self._open_view(key, channel)
        if opened is None:
            return None

        view, size, meta = opened

        try:
            if offset < 0:
                return view.slice_before(size, JournalWindow.BYTES, size, meta)
            return view.slice_at(offset, JournalWindow.BYTES, size, meta)
        except OSError as exc:
            raise StreamJournalError(
                f"stream log is not readable: {key.call_id}/{channel}: {exc}"
            ) from exc

    def slice_before(
        self, key: StreamKey, end: int, channel: JournalChannel
    ) -> StreamSlice | None:
        """Окно, заканчивающееся на end: прокрутка вверх стыкуется встык."""
        opened = self._open_view(key, channel)
        if opened is None:
            return None

        view, size, meta = opened

        try:
            return view.slice_before(end, JournalWindow.BYTES, size, meta)
        except OSError as exc:
            raise StreamJournalError(
                f"stream log is not readable: {key.call_id}/{channel}: {exc}"
            ) from exc

    def stat_of(self, key: StreamKey, channel: JournalChannel) -> StreamStat | None:
        """Размер и итог журнала без чтения тела; None — журнала нет."""
        opened = self._open_view(key, channel)
        if opened is None:
            return None

        _, size, meta = opened

        return StreamStat(size=size, closed=meta.closed, note=meta.note)

    def channels_of(self, key: StreamKey) -> tuple[JournalChannel, ...]:
        """Каналы вызова, у которых есть файл; порядок — канонический.

        Пустой кортеж — журнала вызова нет вовсе либо его вытеснила ротация.
        """
        root = self._vault.root_for(key.user_id)
        thread_path = VaultPath.inside(root, key.thread_id)

        written = set(self._written_channels(thread_path, key.call_id))

        found: list[JournalChannel] = []
        for channel in JournalChannels.order():
            if channel.value not in written:
                continue

            found.append(channel)

        return tuple(found)

    @staticmethod
    def _written_channels(thread_path: str, call_id: str) -> Iterator[str]:
        """Имена каналов из файлов вызова; каталога нет — ничего не записано."""
        try:
            entries = list(os.scandir(thread_path))
        except OSError:
            return

        for file_entry in entries:
            if not file_entry.is_file(follow_symlinks=False):
                continue

            if not JournalFile.is_log(file_entry.name):
                continue

            parsed = JournalFile.parse_log(file_entry.name)
            if parsed.call_id != call_id:
                continue

            yield parsed.channel

    def log_rel_path(self, key: StreamKey, channel: JournalChannel) -> str | None:
        """Rel-путь лога канала; имя инструмента берётся из сайдкара вызова.

        None — вызов не журналировался (нет сайдкара) либо файла канала нет.
        """
        root = self._vault.root_for(key.user_id)
        meta = self._read_meta(VaultPath.inside(root, key.rel_meta()))
        if not meta.tool_name:
            return None

        rel = key.rel_log(meta.tool_name, channel)
        if not os.path.isfile(VaultPath.inside(root, rel)):
            return None

        return rel

    def _open_view(
        self, key: StreamKey, channel: JournalChannel
    ) -> tuple[StreamFileView, int, StreamMeta] | None:
        root = self._vault.root_for(key.user_id)

        meta = self._read_meta(VaultPath.inside(root, key.rel_meta()))
        if not meta.tool_name:
            return None

        log_path = VaultPath.inside(root, key.rel_log(meta.tool_name, channel))

        try:
            size = os.stat(log_path).st_size
        except FileNotFoundError:
            return None

        return StreamFileView(log_path), size, meta

    def vault_root(self, user_id: str) -> str:
        """Корень тома пользователя: под ним лежат файлы каналов вызовов.

        Отдаётся скачиванию журнала — файл течёт из тома тем же StreamedFile,
        что и вложения.
        """
        return self._vault.root_for(user_id)

    @staticmethod
    def _read_meta(meta_path: str) -> StreamMeta:
        """Сайдкар потерян или бит — журнал считается закрытым без итога."""
        try:
            with open(meta_path, encoding=JournalText.ENCODING) as f:
                raw = json.load(f)
            return StreamMeta.model_validate(raw)
        except (OSError, ValueError):
            return StreamMeta(tool_name="", closed=True, note="")
