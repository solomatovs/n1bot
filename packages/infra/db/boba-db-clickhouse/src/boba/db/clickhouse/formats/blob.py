"""Формат RawBLOB: значения всех строк и всех колонок подряд, без разделителей
и без экранирования; строки как есть, числа двоичными little-endian, NULL и
пустой результат — ноль байт. Границ значений в потоке нет, поэтому формат
для одной колонки и одного значения: файла, картинки, документа."""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from typing import Any, ClassVar

from boba.db.clickhouse.formats.base import Blocks, StreamFormat
from boba.db.clickhouse.formats.lines import Lines, Settings

__all__ = ["RawBlob", "RawBlobStream"]


@dataclass(frozen=True)
class RawBlobStream:
    """Поток RawBLOB: байты одного значения как есть."""

    blocks: AsyncIterator[memoryview]


class RawBlob(StreamFormat[RawBlobStream]):
    """Реализация StreamFormat для RawBLOB: разбирать нечего, read и write
    отдают байты как есть."""

    FORMAT: ClassVar[str] = "RawBLOB"

    def __init__(self) -> None:
        self._output = Settings({})
        self._input = Settings({})

    def output_settings(self, extra: Mapping[str, Any] | None) -> dict[str, Any]:
        return self._output.merged(extra)

    def input_settings(self, extra: Mapping[str, Any] | None) -> dict[str, Any]:
        return self._input.merged(extra)

    def insert(self, query: str) -> str:
        return f"{query}\n FORMAT {self.FORMAT}"

    async def read(self, blocks: Blocks) -> RawBlobStream:
        return RawBlobStream(blocks=Lines.views(blocks))

    def write(self, stream: RawBlobStream) -> AsyncIterator[memoryview]:
        return stream.blocks
