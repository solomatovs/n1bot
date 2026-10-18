"""Приём живого вывода инструмента: интерфейс приёмника и контекстный тап.

Пока тело инструмента работает, его вывод читается порциями и куда-то
складывается: в файл журнала, в буфер вызова. Здесь объявлен общий
интерфейс такого приёмника (StreamSink) и протокол приёмников журнала
(ChannelSinks) — способ передать приёмники журнала из UI-слоя в исполнителя
через обстановку вызова (CallAmbient), не связывая слои импортами.

Ошибки: наружу ничего не выходит; on_data обязан не поднимать исключений.
"""

from __future__ import annotations

from abc import abstractmethod
from collections.abc import Callable
from typing import Protocol, TypeAlias

from boba.toolkit.channels import JournalChannel

__all__ = [
    "ChannelSinks",
    "Chunk",
    "ChunkSink",
    "StreamSink",
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


class ChannelSinks(Protocol):
    """Протокол журнала одного вызова: по каналу отдаёт его приёмник.
    Реализация — ToolStream в boba.toolrun.streams; в исполнитель попадает
    через CallAmbient."""

    @abstractmethod
    def sink_of(self, channel: JournalChannel) -> StreamSink: ...
