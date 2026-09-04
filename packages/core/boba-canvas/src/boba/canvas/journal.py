"""Журнал потоков вызовов: имена файлов, ключи и метаданные потоков, окна чтения,
учёт места, порты записи и чтения; файловая реализация — в приложении.

Ошибки:
StreamJournalError — журнал недоступен или запись/окно нарушают контракт.
"""

from __future__ import annotations

import os
import re
from abc import abstractmethod
from collections.abc import Callable
from enum import IntEnum, StrEnum
from typing import ClassVar, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator

from boba.canvas.canvas import WatchProbe
from boba.toolkit.channels import JournalChannel
from boba.toolkit.stream import StreamSink

__all__ = [
    "CallLogUsage",
    "CallStream",
    "ChannelProbe",
    "JournalFile",
    "JournalText",
    "JournalWindow",
    "LogName",
    "PathSegment",
    "StreamJournalError",
    "StreamJournalHub",
    "StreamKey",
    "StreamMeta",
    "StreamNote",
    "StreamRecorderPort",
    "StreamSlice",
    "StreamStat",
    "StreamStorePort",
    "ThreadUsage",
    "VaultUsage",
    "WindowAlign",
]


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
            msg = (
                f"journal log name for call {call_id!r} and tool {tool!r}: "
                "segments must not contain dots"
            )
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
            msg = (
                f"unsafe journal path segment {value!r}: "
                f"expected to match {cls.SAFE.pattern}"
            )
            raise ValueError(msg)

        return value


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
            msg = f"stream key call_id {value!r} must not contain dots"
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


class StreamNote(StrEnum):
    """Экранные тексты статусной строки окна потока.

    Исходы закрытого вызова приходят из журнала словами CallOutcome —
    их пишет обвязка вызова, панель показывает как есть.
    """

    RUNNING = "running…"
    GONE = "The log of this call is unavailable: journaling was not active."

    @classmethod
    def status_of(cls, piece: StreamSlice) -> str:
        if not piece.closed:
            return str(cls.RUNNING)

        return piece.note


class ChannelProbe(BaseModel):
    """Состояние одного канала живого вызова: что насос сравнивает между записями."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    channel: JournalChannel
    probe: WatchProbe


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


class CallStream(Protocol):
    """Журнал живого вывода одного вызова: приёмники каналов и закрытие."""

    @abstractmethod
    def sink_of(self, channel: JournalChannel) -> StreamSink: ...

    @abstractmethod
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
