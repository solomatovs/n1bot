"""Живой вывод инструмента: приёмник байтов, кольцевое окно и контекстный тап.

Пишущая сторона (поток процесса) отдаёт байты в StreamSink; куда они едут —
в кольцевое окно памяти или в файл журнала — решает владелец приёмника.
Тап передаёт приёмник из UI-слоя в исполнителя через contextvar, не связывая
слои импортами.

Ошибки: наружу ничего не выходит; on_data обязан не поднимать исключений.
"""

from __future__ import annotations

import threading
from abc import abstractmethod
from collections import deque
from collections.abc import Callable
from contextvars import ContextVar, Token
from typing import ClassVar, Protocol

from pydantic import BaseModel, ConfigDict

from boba.toolkit.channels import ToolChannel

__all__ = [
    "ChannelSinks",
    "StreamSink",
    "StreamWindow",
    "ToolCallContext",
    "ToolCallInfo",
    "ToolChannelsTap",
    "ToolStreamBuffer",
    "ToolStreamTap",
]


class StreamSink(Protocol):
    """Приёмник живого вывода; запись не блокирует и не поднимает исключений."""

    @abstractmethod
    def feed(self, data: bytes) -> None: ...

    @abstractmethod
    def feed_text(self, text: str) -> None: ...


class StreamWindow(BaseModel):
    """Снапшот окна вывода: хвост, объём вытесненного и признак завершения."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    text: str
    dropped_bytes: int
    closed: bool
    note: str


class ToolStreamBuffer:
    """Кольцевое окно байтов вывода: запись из потока инструмента без блокировок.

    feed и close зовут on_data после отпускания замка: колбэк будит читателя
    и обязан быть быстрым и потокобезопасным.
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

    def feed(self, data: bytes) -> None:
        """Принять порцию вывода; лишнее вытесняется с головы окна."""
        if not data:
            return

        with self._lock:
            if self._closed:
                return
            self._chunks.append(data)
            self._size += len(data)
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


class ToolStreamTap:
    """Приёмник живого вывода в текущем контексте выполнения инструмента."""

    _SINK: ClassVar[ContextVar[StreamSink | None]] = ContextVar(
        "tool_stream_tap", default=None
    )

    @classmethod
    def set(cls, sink: StreamSink | None) -> None:
        cls._SINK.set(sink)

    @classmethod
    def get(cls) -> StreamSink | None:
        return cls._SINK.get()


class ChannelSinks(Protocol):
    """Приёмники журнала каналов одного вызова."""

    @abstractmethod
    def sink_of(self, channel: ToolChannel) -> StreamSink: ...


class ToolChannelsTap:
    """Приёмники каналов текущего вызова: обвязка ставит, исполнитель читает."""

    _SINKS: ClassVar[ContextVar[ChannelSinks | None]] = ContextVar(
        "tool_channels_tap", default=None
    )

    @classmethod
    def set(cls, sinks: ChannelSinks | None) -> None:
        cls._SINKS.set(sinks)

    @classmethod
    def get(cls) -> ChannelSinks | None:
        return cls._SINKS.get()


class ToolCallInfo(BaseModel):
    """Вызов инструмента: имя и идентификатор из протокола LLM-провайдера."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    call_id: str = ""
    """Пустая строка — вызов пришёл без tool_call_id (прямой вызов, тест)."""


class ToolCallContext:
    """Текущий вызов инструмента в контексте выполнения.

    Ставит обвязка вызова, читает исполнитель — метка идёт в его логи.
    """

    _CALL: ClassVar[ContextVar[ToolCallInfo | None]] = ContextVar(
        "tool_call_info", default=None
    )

    @classmethod
    def set(cls, call: ToolCallInfo) -> Token[ToolCallInfo | None]:
        return cls._CALL.set(call)

    @classmethod
    def reset(cls, token: Token[ToolCallInfo | None]) -> None:
        cls._CALL.reset(token)

    @classmethod
    def get(cls) -> ToolCallInfo | None:
        return cls._CALL.get()

    @classmethod
    def name(cls) -> str:
        """Имя текущего вызова; пустая строка — вызова в контексте нет."""
        call = cls._CALL.get()
        if call is None:
            return ""

        return call.name
