"""Порты насосов: в памяти (выход копит тела кадров, вход отдаёт их порциями)
и труба ОС, через которую два насоса работают одновременно."""

from __future__ import annotations

import os
from collections.abc import Iterator
from typing import Any

from boba.toolkit.frames import ToolIo
from boba.toolkit.ports import Chunk, Framed, Outbound, StreamPorts
from boba.toolkit.transfer import (
    Engine,
    RawHead,
    RowsHead,
    SchemaHead,
    TransferFrame,
)

__all__ = ["Feed", "Pipe", "Sink"]


class Sink:
    """Выходной порт кадров в память: копит тела кадров rows, кадр schema
    запоминает."""

    def __init__(self) -> None:
        self._buffer = bytearray()
        self.heads: list[Any] = []

    def emit(self, head: Any, body: Any = b"") -> None:
        self.heads.append(head)
        self._buffer.extend(memoryview(body))

    def data(self) -> bytes:
        return bytes(self._buffer)


class SinkOutbound(Outbound[TransferFrame]):
    """Порт Outbound поверх Sink: для прямых вызовов источников в тестах,
    минуя ToolMain."""

    def __init__(self, sink: Sink) -> None:
        self._sink = sink

    def emit(self, head: TransferFrame, body: Chunk = b"") -> None:
        self._sink.emit(head, body)


class Feed:
    """Входной порт кадров из памяти: первым кадр schema, дальше порции
    произвольного размера — они режут строки и многобайтовые символы где
    попало."""

    RAW: RawHead = RawHead(kind="raw", source_engine=Engine.POSTGRES)

    def __init__(self, data: bytes, size: int, head: SchemaHead | RawHead) -> None:
        self._data = data
        self._size = size
        self._head = head

    def __iter__(self) -> Iterator[Framed[Any]]:
        yield Framed(head=self._head, body=b"")
        offset = 0
        while offset < len(self._data):
            chunk = self._data[offset : offset + self._size]
            offset += self._size
            yield Framed(head=RowsHead(kind="rows"), body=chunk)


class Pipe:
    """Труба ОС между выходом одного насоса и входом другого, как у лончера:
    выход пишет в конец записи, вход читает конец чтения до EOF. Порты
    строятся по объявлениям тел, как их строит ToolMain. Каждый конец
    закрывает сторона, которая им владеет, когда её насос завершился: так
    вход видит EOF, а выход при упавшем входе получает EPIPE, а не зависает.
    Аннотации портов — как в подписях тел: Inbound[...]/Outbound[...] с
    моделями заголовков."""

    def __init__(self, outbound: Any, inbound: Any) -> None:
        self._read_fd, self._write_fd = os.pipe()
        self.outbound = StreamPorts.build(
            outbound, ToolIo.on_channels(-1, self._write_fd)
        )
        self.inbound = StreamPorts.build(inbound, ToolIo.on_channels(self._read_fd, -1))

    def close_write(self) -> None:
        os.close(self._write_fd)

    def close_read(self) -> None:
        os.close(self._read_fd)
