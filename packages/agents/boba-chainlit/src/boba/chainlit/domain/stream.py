"""Модели журнала вывода инструментов и порты доступа к нему.

Живут ниже хранилища: панель и инструменты работают с журналом через эти
контракты, а файловую реализацию подставляет сборка приложения.

Ошибки: StreamJournalError — том недоступен, файл не открывается или место
кончилось.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from enum import IntEnum, StrEnum
from typing import ClassVar, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator

from boba.toolkit.channels import ToolChannel
from boba.toolkit.stream import StreamSink

__all__ = [
    "CallLogUsage",
    "JournalFile",
    "JournalText",
    "JournalWindow",
    "LogName",
    "StreamJournalError",
    "StreamJournalHub",
    "StreamKey",
    "StreamMeta",
    "StreamRecorderPort",
    "StreamSlice",
    "StreamStorePort",
    "ThreadUsage",
    "VaultUsage",
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
        cls, thread_id: str, call_id: str, tool: str, channel: ToolChannel
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


class StreamKey(BaseModel):
    """Адрес журнала одного вызова: {thread_id}/{call_id} в томе пользователя.

    call_id приходит из протокола LLM-провайдера — в путь допускаются только
    безопасные символы, всё прочее отвергается на границе. Точка в call_id
    запрещена: имя файла {call_id}.{tool}.{channel}.log разбирается по
    сегментам, и точка внутри сделала бы его неразложимым.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    SAFE: ClassVar[frozenset[str]] = frozenset(
        "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-"
    )

    user_id: str = Field(min_length=1)
    thread_id: str = Field(min_length=1)
    call_id: str = Field(min_length=1, max_length=255)

    @field_validator("user_id", "thread_id", "call_id")
    @classmethod
    def _safe_segment(cls, value: str) -> str:
        unsafe = set(value) - cls.SAFE
        if unsafe:
            msg = f"unsafe characters in stream key segment: {sorted(unsafe)}"
            raise ValueError(msg)

        if value.startswith("."):
            msg = f"stream key segment must not start with a dot: {value!r}"
            raise ValueError(msg)

        return value

    @field_validator("call_id")
    @classmethod
    def _no_dots(cls, value: str) -> str:
        if "." in value:
            msg = f"call_id must not contain dots: {value!r}"
            raise ValueError(msg)

        return value

    def rel_log(self, tool: str, channel: ToolChannel) -> str:
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
    """Писатель журнала одного вызова: панели нужен хвост и закрытие."""

    @property
    def closed(self) -> bool: ...

    def tail(self, window: int) -> StreamSlice: ...

    def close(self, note: str) -> None: ...


class StreamStorePort(Protocol):
    """Журнал приложения: открыть писателя канала и прочитать окно."""

    def recorder(
        self,
        key: StreamKey,
        tool_name: str,
        channel: ToolChannel,
        on_data: Callable[[], None],
        protected_prefixes: frozenset[str],
    ) -> StreamRecorderPort: ...

    def slice_at(
        self, key: StreamKey, offset: int, channel: ToolChannel
    ) -> StreamSlice | None: ...

    def slice_before(
        self, key: StreamKey, end: int, channel: ToolChannel
    ) -> StreamSlice | None: ...

    def log_rel_path(self, key: StreamKey, channel: ToolChannel) -> str | None: ...

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
