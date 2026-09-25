"""Порты насосов: в памяти (выход копит байты, вход отдаёт их порциями) и
труба ОС, через которую два насоса работают одновременно."""

from __future__ import annotations

import os
from typing import Any

from boba.toolkit.frames import ToolIo
from boba.toolkit.ports import RawInbound, RawOutbound

__all__ = ["Feed", "Pipe", "Sink"]


class Sink(RawOutbound):
    """Выходной порт в память: копит всё, что записал насос."""

    def __init__(self) -> None:
        super().__init__(ToolIo.detached())
        self._buffer = bytearray()

    def write(self, buffer: Any) -> int:
        chunk = memoryview(buffer)
        self._buffer.extend(chunk)

        return len(chunk)

    def data(self) -> bytes:
        return bytes(self._buffer)


class Feed(RawInbound):
    """Входной порт из памяти: порции произвольного размера режут строки и
    многобайтовые символы где попало."""

    def __init__(self, data: bytes, size: int) -> None:
        super().__init__(ToolIo.detached())
        self._data = data
        self._size = size
        self._offset = 0

    def readinto(self, buffer: Any) -> int:
        """Не больше своего размера за вызов, а не весь buffer: границы порций
        режут данные где попало."""
        target = memoryview(buffer).cast("B")
        size = min(self._size, len(target), len(self._data) - self._offset)
        target[:size] = self._data[self._offset : self._offset + size]
        self._offset += size

        return size


class Pipe:
    """Труба ОС между выходом одного насоса и входом другого, как у лончера:
    выход пишет в конец записи, вход читает конец чтения до EOF. Каждый конец
    закрывает сторона, которая им владеет, когда её насос завершился, — так
    вход видит EOF, а выход при упавшем входе получает EPIPE, а не зависает."""

    def __init__(self) -> None:
        self._read_fd, self._write_fd = os.pipe()
        self.outbound = RawOutbound(ToolIo.on_channels(-1, self._write_fd))
        self.inbound = RawInbound(ToolIo.on_channels(self._read_fd, -1))

    def close_write(self) -> None:
        os.close(self._write_fd)

    def close_read(self) -> None:
        os.close(self._read_fd)
