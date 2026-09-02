"""Приём живого вывода инструмента: интерфейс приёмника и контекстный тап.

Пока тело инструмента работает, его вывод читается порциями и куда-то
складывается: в файл журнала, в кольцевое окно памяти для панели, в буфер
вызова. Здесь объявлен общий интерфейс такого приёмника (StreamSink),
кольцевое окно (ToolStreamBuffer) и тап (ToolChannelsTap) — способ передать
приёмники журнала из UI-слоя в исполнителя через contextvar, не связывая
слои импортами.

Ошибки: наружу ничего не выходит; on_data обязан не поднимать исключений.
"""

from __future__ import annotations

import threading
from abc import abstractmethod
from collections import deque
from collections.abc import Callable
from contextvars import ContextVar
from typing import ClassVar, Protocol, TypeAlias

from pydantic import BaseModel, ConfigDict

from boba.toolkit.channels import JournalChannel

__all__ = [
    "ChannelSinks",
    "Chunk",
    "ChunkSink",
    "StreamSink",
    "StreamWindow",
    "ToolChannelsTap",
    "ToolStreamBuffer",
]

Chunk: TypeAlias = bytes | memoryview
"""Порция байтов канала.

memoryview смотрит в буфер читателя и живёт до следующего чтения того же
дескриптора: приёмник, который порцию хранит, обязан скопировать её сразу
(`bytearray.extend`, `bytes(...)`, запись в файл), а не отложить объект.
"""

ChunkSink: TypeAlias = Callable[[Chunk], None]
"""Куда читатель отдаёт порцию канала."""


class StreamSink(Protocol):
    """Протокол приёмника живого вывода: feed принимает порцию байтов,
    feed_text — строку. Запись не блокирует и не поднимает исключений —
    сюда пишет насос каналов, которому падать нельзя."""

    @abstractmethod
    def feed(self, data: Chunk) -> None: ...

    @abstractmethod
    def feed_text(self, text: str) -> None: ...


class StreamWindow(BaseModel):
    """Снимок кольцевого окна для показа: текст хвоста, сколько байт
    вытеснено и закрыт ли поток."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    text: str
    dropped_bytes: int
    closed: bool
    note: str


class ToolStreamBuffer(StreamSink):
    """Реализация StreamSink кольцевым окном в памяти: помнит хвост вывода
    в пределах лимита, старое вытесняется.

    Из него панель показывает живой вывод инструмента без чтения файлов.
    feed и close зовут on_data после отпускания замка: колбэк будит
    читателя и обязан быть быстрым и потокобезопасным.
    """

    def __init__(self, window_bytes: int, on_data: Callable[[], None]) -> None:
        if window_bytes <= 0:
            msg = f"window_bytes must be positive, got {window_bytes}"
            raise ValueError(msg)

        self._window = window_bytes
        self._on_data = on_data
        self._lock = threading.Lock()
        self._chunks: deque[bytes] = deque()
        self._size = 0
        self._dropped = 0
        self._closed = False
        self._note = ""

    def feed(self, data: Chunk) -> None:
        """Принять порцию вывода; лишнее вытесняется с головы окна."""
        if not data:
            return

        # окно держит порцию до снятия снапшота, а буфер читателя уедет на
        # следующем чтении — копия обязательна
        kept = bytes(data)

        with self._lock:
            if self._closed:
                return
            self._chunks.append(kept)
            self._size += len(kept)
            self._evict()

        self._on_data()

    def feed_text(self, text: str) -> None:
        self.feed(text.encode("utf-8"))

    def close(self, note: str) -> None:
        """Завершить поток; повторное закрытие ничего не меняет."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._note = note

        self._on_data()

    def snapshot(self) -> StreamWindow:
        with self._lock:
            data = b"".join(self._chunks)
            dropped = self._dropped
            closed = self._closed
            note = self._note

        return StreamWindow(
            text=data.decode("utf-8", errors="replace"),
            dropped_bytes=dropped,
            closed=closed,
            note=note,
        )

    def _evict(self) -> None:
        """Держит окно в лимите; зовётся под замком."""
        while self._size > self._window:
            head = self._chunks[0]
            excess = self._size - self._window

            if len(head) <= excess:
                self._chunks.popleft()
                self._size -= len(head)
                self._dropped += len(head)
                continue

            self._chunks[0] = head[excess:]
            self._size -= excess
            self._dropped += excess


class ChannelSinks(Protocol):
    """Протокол журнала одного вызова: по каналу отдаёт его приёмник.
    Реализация — ToolStream в boba.toolrun.streams; в исполнитель попадает
    через ToolChannelsTap."""

    @abstractmethod
    def sink_of(self, channel: JournalChannel) -> StreamSink: ...


class ToolChannelsTap:
    """Contextvar-переноска журнала текущего вызова: обвязка запуска ставит
    ChannelSinks перед вызовом, исполнитель (CallSinks.merged) забирает.

    Нужна, чтобы UI-слой не импортировался исполнителем: связь идёт через
    контекст исполнения, а не через модули.
    """

    _SINKS: ClassVar[ContextVar[ChannelSinks | None]] = ContextVar(
        "tool_channels_tap", default=None
    )

    @classmethod
    def set(cls, sinks: ChannelSinks | None) -> None:
        cls._SINKS.set(sinks)

    @classmethod
    def get(cls) -> ChannelSinks | None:
        return cls._SINKS.get()
