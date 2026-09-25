"""Порты насосов в памяти: выход копит байты, вход отдаёт их порциями."""

from __future__ import annotations

from collections.abc import Iterator

from boba.toolkit.frames import ToolIo
from boba.toolkit.ports import RawInbound, RawOutbound
from boba.toolkit.stream import Chunk

__all__ = ["Feed", "Sink"]


class Sink(RawOutbound):
    """Выходной порт в память: копит всё, что записал насос."""

    def __init__(self) -> None:
        super().__init__(ToolIo.detached())
        self._buffer = bytearray()

    async def write(self, chunk: Chunk) -> None:
        self._buffer.extend(chunk)

    def data(self) -> bytes:
        return bytes(self._buffer)


class Feed(RawInbound):
    """Входной порт из памяти: порции произвольного размера режут строки и
    многобайтовые символы где попало."""

    def __init__(self, data: bytes, size: int) -> None:
        super().__init__(ToolIo.detached())
        self._data = data
        self._size = size

    def read(self, chunk_bytes: int) -> Iterator[bytes]:
        for start in range(0, len(self._data), self._size):
            yield self._data[start : start + self._size]
