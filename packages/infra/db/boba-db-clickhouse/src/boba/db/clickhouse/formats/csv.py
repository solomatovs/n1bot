"""Форматы CSV: без шапки (CSV), с именами колонок в первой записи
(CSVWithNames) и с именами и типами в первых двух (CSVWithNamesAndTypes).
Шапка квотирована по RFC 4180: поле в двойных кавычках, кавычка внутри
удваивается, перевод строки внутри имени идёт сырым; поэтому границу записи
шапки ищет разборщик CSV, а не перевод строки. NULL в данных — \\N.

Ошибки:
ClickHouseFormatError — поток оборвался до шапки, шапка не разбирается как
    CSV или в ней незнакомый тип.
"""

from __future__ import annotations

import codecs
import csv
import io
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, ClassVar

from clickhouse_connect.datatypes.base import ClickHouseType

from boba.db.clickhouse.errors import ClickHouseFormatError
from boba.db.clickhouse.formats.base import Blocks, StreamFormat
from boba.db.clickhouse.formats.lines import ColumnTypes, HeadLines, Lines, Settings

__all__ = [
    "Csv",
    "CsvNamesStream",
    "CsvStream",
    "CsvTypedStream",
    "CsvWithNames",
    "CsvWithNamesAndTypes",
]


@dataclass(frozen=True)
class CsvStream:
    """Поток CSV без шапки: байты записей как есть."""

    blocks: AsyncIterator[memoryview]


@dataclass(frozen=True)
class CsvNamesStream:
    """Поток CSVWithNames без шапки: имена колонок и байты записей."""

    names: tuple[str, ...]
    blocks: AsyncIterator[memoryview]


@dataclass(frozen=True)
class CsvTypedStream:
    """Поток CSVWithNamesAndTypes без шапки: имена колонок, типы так, как их
    написал сервер (они же уходят обратно в шапку при записи), те же типы
    объектами драйвера и байты записей."""

    names: tuple[str, ...]
    type_names: tuple[str, ...]
    column_types: tuple[ClickHouseType, ...]
    blocks: AsyncIterator[memoryview]


class CsvHeader:
    """Записи шапки CSV: разбор стандартным csv строгим ридером и запись
    csv.writer со всеми полями в кавычках, как пишет сервер."""

    ENCODING: ClassVar[str] = "utf-8"
    LINE_END: ClassVar[str] = "\n"

    def parse(self, record: bytes) -> tuple[str, ...]:
        text = record.decode(self.ENCODING)
        reader = csv.reader(io.StringIO(text, newline=""), strict=True)

        return tuple(next(reader))

    def render(self, values: Sequence[str]) -> bytes:
        buffer = io.StringIO(newline="")
        writer = csv.writer(buffer, quoting=csv.QUOTE_ALL, lineterminator=self.LINE_END)
        writer.writerow(values)

        return buffer.getvalue().encode(self.ENCODING)


class CsvHead:
    """Снимает заданное число записей CSV с начала байтового потока. Запись
    может занимать несколько строк, поэтому буфер разбирается строгим
    csv.reader: он либо отдаёт запись целиком, либо просит ещё данных;
    граница в байтах — по числу потреблённых им строк. Остальное идёт
    дальше как есть."""

    def __init__(self, fmt: str, count: int) -> None:
        self._fmt = fmt
        self._count = count

    async def take(self, chunks: AsyncIterator[memoryview]) -> HeadLines:
        head = bytearray()
        decoder = codecs.getincrementaldecoder(CsvHeader.ENCODING)()
        text = ""
        while True:
            consumed = self._consumed(text)
            if consumed is not None:
                records = self._records(consumed)
                rest = bytes(head[len(consumed.encode(CsvHeader.ENCODING)) :])
                return HeadLines(lines=tuple(records), rest=rest)

            chunk = await anext(chunks, None)
            if chunk is None:
                raise ClickHouseFormatError(
                    f"reading {self._fmt} header: expected {self._count} header "
                    f"records, the stream ended after {len(head)} bytes"
                )

            head.extend(chunk)
            text += decoder.decode(chunk)

    def _consumed(self, text: str) -> str | None:
        """Текст первых count записей с переводами строки, или None, если в
        буфере их ещё нет целиком. Строки считаются так же, как их читает
        csv.reader: по правилам StringIO без перевода newline."""
        lines = io.StringIO(text, newline="").readlines()
        reader = csv.reader(io.StringIO(text, newline=""), strict=True)
        try:
            for _ in range(self._count):
                next(reader)
        except (StopIteration, csv.Error):
            return None

        consumed = "".join(lines[: reader.line_num])
        if not consumed.endswith(CsvHeader.LINE_END):
            return None

        return consumed

    def _records(self, consumed: str) -> list[bytes]:
        """Записи шапки по отдельности, байтами без перевода строки."""
        lines = io.StringIO(consumed, newline="").readlines()
        records: list[bytes] = []
        start = 0
        reader = csv.reader(io.StringIO(consumed, newline=""), strict=True)
        for _ in range(self._count):
            next(reader)
            record = "".join(lines[start : reader.line_num])
            records.append(record.rstrip(CsvHeader.LINE_END).encode(CsvHeader.ENCODING))
            start = reader.line_num

        return records


class Csv(StreamFormat[CsvStream]):
    """Реализация StreamFormat для CSV без шапки: разбирать нечего, read и
    write отдают байты как есть. На вставке колонки идут по позиции;
    у новых серверов input_format_csv_detect_header = 1 по умолчанию, и
    первая запись, похожая на имена колонок таблицы, молча пропускается —
    у старых такой настройки нет, поэтому формат её не ставит."""

    FORMAT: ClassVar[str] = "CSV"

    def __init__(self) -> None:
        self._output = Settings({})
        self._input = Settings({})

    def output_settings(self, extra: Mapping[str, Any] | None) -> dict[str, Any]:
        return self._output.merged(extra)

    def input_settings(self, extra: Mapping[str, Any] | None) -> dict[str, Any]:
        return self._input.merged(extra)

    def insert(self, query: str) -> str:
        return f"{query}\n FORMAT {self.FORMAT}"

    async def read(self, blocks: Blocks) -> CsvStream:
        return CsvStream(blocks=Lines.views(blocks))

    def write(self, stream: CsvStream) -> AsyncIterator[memoryview]:
        return stream.blocks


class CsvWithNames(StreamFormat[CsvNamesStream]):
    """Реализация StreamFormat для CSVWithNames: read снимает запись с
    именами колонок и отдаёт остальные байты как есть, write ставит её
    перед байтами потока. Сервер на вставке сопоставляет колонки по именам
    и по умолчанию молча пропускает лишнюю (input_format_skip_unknown_fields)."""

    FORMAT: ClassVar[str] = "CSVWithNames"

    def __init__(self) -> None:
        self._header = CsvHeader()
        self._head = CsvHead(self.FORMAT, 1)
        self._output = Settings({})
        self._input = Settings({})

    def output_settings(self, extra: Mapping[str, Any] | None) -> dict[str, Any]:
        return self._output.merged(extra)

    def input_settings(self, extra: Mapping[str, Any] | None) -> dict[str, Any]:
        return self._input.merged(extra)

    def insert(self, query: str) -> str:
        return f"{query}\n FORMAT {self.FORMAT}"

    async def read(self, blocks: Blocks) -> CsvNamesStream:
        chunks = Lines.views(blocks)
        head = await self._head.take(chunks)

        return CsvNamesStream(
            names=self._header.parse(head.lines[0]),
            blocks=Lines.all(head.rest, chunks),
        )

    def write(self, stream: CsvNamesStream) -> AsyncIterator[memoryview]:
        return Lines.all(self._header.render(stream.names), stream.blocks)


class CsvWithNamesAndTypes(StreamFormat[CsvTypedStream]):
    """Реализация StreamFormat для CSVWithNamesAndTypes: read снимает записи
    с именами и типами колонок, строит типы драйвером и отдаёт остальные
    байты как есть; write ставит обе записи перед байтами потока. Сервер на
    вставке сопоставляет колонки по именам, отклоняет несовпавший тип и по
    умолчанию молча пропускает лишнюю колонку."""

    FORMAT: ClassVar[str] = "CSVWithNamesAndTypes"

    def __init__(self) -> None:
        self._header = CsvHeader()
        self._head = CsvHead(self.FORMAT, 2)
        self._types = ColumnTypes(self.FORMAT)
        self._output = Settings({})
        self._input = Settings({})

    def output_settings(self, extra: Mapping[str, Any] | None) -> dict[str, Any]:
        return self._output.merged(extra)

    def input_settings(self, extra: Mapping[str, Any] | None) -> dict[str, Any]:
        return self._input.merged(extra)

    def insert(self, query: str) -> str:
        return f"{query}\n FORMAT {self.FORMAT}"

    async def read(self, blocks: Blocks) -> CsvTypedStream:
        chunks = Lines.views(blocks)
        head = await self._head.take(chunks)
        names = self._header.parse(head.lines[0])
        type_names = self._header.parse(head.lines[1])

        return CsvTypedStream(
            names=names,
            type_names=type_names,
            column_types=self._types.of(type_names),
            blocks=Lines.all(head.rest, chunks),
        )

    def write(self, stream: CsvTypedStream) -> AsyncIterator[memoryview]:
        head = self._header.render(stream.names) + self._header.render(
            stream.type_names
        )

        return Lines.all(head, stream.blocks)
