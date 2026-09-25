"""Форматы Apache Arrow: файловый Arrow и потоковый ArrowStream. Оба
начинаются со схемы: у ArrowStream это первое IPC-сообщение, у Arrow перед
ним стоит магия ARROW1, а в конце файла лежит footer со смещениями блоков.
Схему читает pyarrow, имена и типы колонок — из неё; байты схемы
сохраняются как есть, и write ставит их обратно ровно такими, поэтому
смещения footer у файлового варианта остаются верными. Сервер отдаёт оба
варианта потоком и принимает потоком.

Ошибки:
ClickHouseFormatError — поток оборвался до конца схемы, начало не ложится в
    раскладку Arrow или схема не разбирается.
"""

from __future__ import annotations

import struct
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, ClassVar

import pyarrow as pa
from pyarrow import ipc

from boba.db.clickhouse.errors import ClickHouseFormatError
from boba.db.clickhouse.formats.base import Blocks, StreamFormat
from boba.db.clickhouse.formats.lines import Lines, Settings

__all__ = ["ArrowColumns", "ArrowFile", "ArrowStream"]


@dataclass(frozen=True)
class ArrowColumns:
    """Поток Arrow без схемы: имена колонок, схема pyarrow с типами Arrow,
    байты схемы (с магией у файлового варианта), которые write ставит
    обратно, и байты блоков записей после неё (у файлового варианта — с
    footer и магией в конце)."""

    names: tuple[str, ...]
    schema: pa.Schema
    head: bytes
    blocks: AsyncIterator[memoryview]


class ArrowOutput(StrEnum):
    """Настройки вывода Arrow, одинаковые у старых и новых серверов: строки
    типом string, а не binary (у 22.x по умолчанию binary)."""

    STRING_AS_STRING = "output_format_arrow_string_as_string"

    def value_of(self) -> int:
        return 1

    @classmethod
    def exact(cls) -> dict[str, Any]:
        """Все настройки вывода со значениями."""
        chosen: dict[str, Any] = {}
        for setting in cls:
            chosen[setting.value] = setting.value_of()

        return chosen


class SchemaHead:
    """Снимает схему с начала байтового потока Arrow: магию заданной длины,
    затем IPC-сообщение схемы по его рамке (маркер продолжения, длина
    метаданных); остальное идёт дальше как есть."""

    CONTINUATION: ClassVar[bytes] = b"\xff\xff\xff\xff"
    FRAME: ClassVar[int] = 8

    def __init__(self, fmt: str, magic: bytes) -> None:
        self._fmt = fmt
        self._magic = magic

    async def take(self, chunks: AsyncIterator[memoryview]) -> ArrowColumns:
        buffer = bytearray()
        while True:
            parsed = self._parsed(bytes(buffer), chunks)
            if parsed is not None:
                return parsed

            chunk = await anext(chunks, None)
            if chunk is None:
                raise ClickHouseFormatError(
                    f"reading {self._fmt} schema: expected the schema message, "
                    f"the stream ended after {len(buffer)} bytes"
                )

            buffer.extend(chunk)

    def _parsed(
        self, buffer: bytes, chunks: AsyncIterator[memoryview]
    ) -> ArrowColumns | None:
        start = len(self._magic)
        if len(buffer) < start + self.FRAME:
            return None

        if not buffer.startswith(self._magic):
            raise ClickHouseFormatError(
                f"reading {self._fmt} schema: expected magic {self._magic!r}, "
                f"got {buffer[:start]!r}"
            )

        marker = buffer[start : start + 4]
        if marker != self.CONTINUATION:
            raise ClickHouseFormatError(
                f"reading {self._fmt} schema: expected the IPC continuation "
                f"marker, got {marker!r}"
            )

        (length,) = struct.unpack("<i", buffer[start + 4 : start + self.FRAME])
        end = start + self.FRAME + length
        if len(buffer) < end:
            return None

        reader = pa.BufferReader(buffer[start:])
        try:
            message = ipc.read_message(reader)
            schema = ipc.read_schema(message)
        except (pa.ArrowException, EOFError) as exc:
            raise ClickHouseFormatError(
                f"reading {self._fmt} schema: expected an Arrow schema message, "
                f"got {buffer[start : start + 20]!r}: {exc}"
            ) from exc

        consumed = start + reader.tell()

        return ArrowColumns(
            names=tuple(schema.names),
            schema=schema,
            head=buffer[:consumed],
            blocks=Lines.all(buffer[consumed:], chunks),
        )


class ArrowStream(StreamFormat[ArrowColumns]):
    """Реализация StreamFormat для ArrowStream: read снимает сообщение схемы
    и отдаёт блоки записей как есть, write ставит её обратно. Типы, которых
    Arrow не знает (Enum, UUID у старых серверов), сервер выводить
    отказывается."""

    FORMAT: ClassVar[str] = "ArrowStream"
    MAGIC: ClassVar[bytes] = b""

    def __init__(self) -> None:
        self._head = SchemaHead(self.FORMAT, self.MAGIC)
        self._output = Settings(ArrowOutput.exact())
        self._input = Settings({})

    def output_settings(self, extra: Mapping[str, Any] | None) -> dict[str, Any]:
        return self._output.merged(extra)

    def input_settings(self, extra: Mapping[str, Any] | None) -> dict[str, Any]:
        return self._input.merged(extra)

    def insert(self, query: str) -> str:
        return f"{query}\n FORMAT {self.FORMAT}"

    async def read(self, blocks: Blocks) -> ArrowColumns:
        return await self._head.take(Lines.views(blocks))

    def write(self, stream: ArrowColumns) -> AsyncIterator[memoryview]:
        return Lines.all(stream.head, stream.blocks)


class ArrowFile(StreamFormat[ArrowColumns]):
    """Реализация StreamFormat для файлового Arrow: read проверяет магию,
    снимает сообщение схемы и отдаёт остальное как есть, включая footer и
    магию в конце; write ставит магию и схему обратно теми же байтами,
    поэтому смещения footer остаются верными."""

    FORMAT: ClassVar[str] = "Arrow"
    MAGIC: ClassVar[bytes] = b"ARROW1\x00\x00"

    def __init__(self) -> None:
        self._head = SchemaHead(self.FORMAT, self.MAGIC)
        self._output = Settings(ArrowOutput.exact())
        self._input = Settings({})

    def output_settings(self, extra: Mapping[str, Any] | None) -> dict[str, Any]:
        return self._output.merged(extra)

    def input_settings(self, extra: Mapping[str, Any] | None) -> dict[str, Any]:
        return self._input.merged(extra)

    def insert(self, query: str) -> str:
        return f"{query}\n FORMAT {self.FORMAT}"

    async def read(self, blocks: Blocks) -> ArrowColumns:
        return await self._head.take(Lines.views(blocks))

    def write(self, stream: ArrowColumns) -> AsyncIterator[memoryview]:
        return Lines.all(stream.head, stream.blocks)
