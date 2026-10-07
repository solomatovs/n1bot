"""Чтение и запись потока Arrow IPC через порты ArrowInbound и ArrowOutbound:
тело насоса получает и отдаёт пачки записей, а не байты. Модуль тянет
pyarrow (extra `arrow`), поэтому импортируется телом инструмента при вызове,
а не модулем объявлений.

Ошибки:
ArrowStreamError — байты входа не читаются как поток Arrow IPC или
    оборвались посреди пачки.
"""

from __future__ import annotations

import asyncio
import io
import os
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass

import pyarrow
import pyarrow.ipc

from boba.toolkit.contract import (
    ArrowFieldMeta,
    ColumnSpec,
    ColumnType,
    TimeUnit,
    TypeFamily,
)
from boba.toolkit.ports import ArrowStreamError

__all__ = [
    "ArrowColumns",
    "ArrowIpc",
    "ArrowReader",
    "ArrowWriter",
    "BytePipe",
    "SourceFields",
]

TypeCheck = Callable[[pyarrow.DataType], bool]
TypeDescriber = Callable[[TypeFamily, pyarrow.DataType, str], ColumnType]


@dataclass(frozen=True)
class ArrowReader:
    """Открытый входной поток: схема из его начала и пачки по мере чтения."""

    schema: pyarrow.Schema
    batches: AsyncIterator[pyarrow.RecordBatch]


class ArrowWriter:
    """Открытый выходной поток: пачки и таблицы уходят в порт по мере записи
    порциями буфера, close пишет конец потока и досылает остаток буфера.
    Запись блокирующая и идёт в потоке. Поток, не доведённый до close,
    остаток буфера в порт не досылает: после ошибки в порт не должно уйти
    ни байта."""

    def __init__(
        self,
        writer: pyarrow.ipc.RecordBatchStreamWriter,
        buffered: io.BufferedIOBase,
        port: io.RawIOBase,
    ) -> None:
        self._writer = writer
        self._buffered = buffered
        self._port = port

    async def write(self, batch: pyarrow.RecordBatch | pyarrow.Table) -> None:
        """Пачка записей или таблица той же схемы."""
        try:
            await asyncio.to_thread(self._writer.write, batch)
        except BaseException:
            self._port.close()
            raise

    async def close(self) -> None:
        try:
            await asyncio.to_thread(self._finish)
        except BaseException:
            self._port.close()
            raise

    def _finish(self) -> None:
        self._writer.close()
        self._buffered.flush()


class ArrowIpc:
    """Открывает потоки IPC над портами. На входе поверх сырого порта стоит
    io.BufferedReader с одним переиспользуемым буфером buffer_bytes: читатель
    IPC ждёт от read(n) ровно n байт, а сырой порт отдаёт короткие чтения.
    На выходе поверх порта стоит io.BufferedWriter того же размера:
    писатель IPC делает по записи на каждый буфер колонки, и без него
    каждая такая запись уходила бы в порт отдельным мелким кадром. Чтение
    и запись блокирующие и идут в потоке."""

    async def open_in(self, port: io.RawIOBase, buffer_bytes: int) -> ArrowReader:
        buffered = io.BufferedReader(port, buffer_bytes)
        try:
            reader = await asyncio.to_thread(pyarrow.ipc.open_stream, buffered)
        except pyarrow.ArrowException as exc:
            raise ArrowStreamError(
                f"reading an arrow ipc stream schema failed: {type(exc).__name__}: "
                f"{exc}"
            ) from exc

        return ArrowReader(schema=reader.schema, batches=self._batches(reader))

    async def open_out(
        self, port: io.RawIOBase, schema: pyarrow.Schema, buffer_bytes: int
    ) -> ArrowWriter:
        buffered = io.BufferedWriter(port, buffer_size=buffer_bytes)
        writer = await asyncio.to_thread(pyarrow.ipc.new_stream, buffered, schema)

        return ArrowWriter(writer, buffered, port)

    async def _batches(
        self, reader: pyarrow.ipc.RecordBatchStreamReader
    ) -> AsyncIterator[pyarrow.RecordBatch]:
        while True:
            try:
                batch = await asyncio.to_thread(self._next, reader)
            except pyarrow.ArrowException as exc:
                raise ArrowStreamError(
                    f"reading an arrow ipc batch failed: {type(exc).__name__}: {exc}"
                ) from exc

            if batch is None:
                return

            yield batch

    @staticmethod
    def _next(
        reader: pyarrow.ipc.RecordBatchStreamReader,
    ) -> pyarrow.RecordBatch | None:
        try:
            return reader.read_next_batch()
        except StopIteration:
            return None


class BytePipe:
    """Труба ОС между производителем байтов и синхронным читателем в потоке
    (читатель Arrow IPC или CSV pyarrow ждёт файл): корутина пишет блоки в
    конец записи через write, писатель рабочего потока — в файл sink;
    читатель читает конец чтения. Оба конца закрывает владелец, когда его
    сторона завершилась."""

    def __init__(self) -> None:
        self._read_fd, self._write_fd = os.pipe()
        self.source = os.fdopen(self._read_fd, "rb", buffering=0)

    async def write(self, block: bytes | bytearray | memoryview) -> None:
        await asyncio.to_thread(self._write_all, memoryview(block))

    def sink(self, buffer_bytes: int) -> io.BufferedWriter:
        """Конец записи файлом с буфером buffer_bytes: для писателя, который
        работает в рабочем потоке сам (цикл libpq) и пишет мелкими порциями.
        Конец записи остаётся за владельцем трубы: закрывает его close_write."""
        raw = io.FileIO(self._write_fd, "wb", closefd=False)

        return io.BufferedWriter(raw, buffer_size=buffer_bytes)

    def close_write(self) -> None:
        os.close(self._write_fd)

    def close_read(self) -> None:
        self.source.close()

    def _write_all(self, block: memoryview) -> None:
        written = 0
        while written < len(block):
            written += os.write(self._write_fd, block[written:])


class SourceFields:
    """Поля схемы потока со стороны источника: тип Arrow плюс metadata
    контракта приёмника (ArrowFieldMeta) — движок источника, текст типа в
    его диалекте и длина строки, если известна."""

    def __init__(self, engine: str) -> None:
        self._engine = engine

    def field(
        self,
        name: str,
        kind: pyarrow.DataType,
        nullable: bool,
        source_type: str,
        char_length: int,
    ) -> pyarrow.Field:
        metadata = {
            ArrowFieldMeta.SOURCE_ENGINE.value: self._engine,
            ArrowFieldMeta.SOURCE_TYPE.value: source_type,
        }
        if char_length > 0:
            metadata[ArrowFieldMeta.CHAR_LENGTH.value] = str(char_length)

        return pyarrow.field(name, kind, nullable=nullable, metadata=metadata)


class ArrowColumns:
    """Схема потока в нейтральные колонки ядра сверки (ColumnSpec): семейство
    и параметры типа — из типа Arrow, текст типа и длина строки — из
    metadata поля, если источник их записал."""

    def specs(self, schema: pyarrow.Schema) -> tuple[ColumnSpec, ...]:
        columns: list[ColumnSpec] = []
        for position, field in enumerate(schema):
            metadata = self._metadata(field)
            columns.append(
                ColumnSpec(
                    name=field.name,
                    kind=self.kind(field.type),
                    nullable=field.nullable,
                    position=position,
                    source_type=metadata.get(ArrowFieldMeta.SOURCE_TYPE.value, ""),
                    char_length=self._length(metadata),
                )
            )

        return tuple(columns)

    def source_engine(self, schema: pyarrow.Schema) -> str:
        """Движок источника из metadata первого поля; пусто, если источник
        его не записал."""
        for field in schema:
            metadata = self._metadata(field)
            return metadata.get(ArrowFieldMeta.SOURCE_ENGINE.value, "")

        return ""

    def kind(self, arrow: pyarrow.DataType) -> ColumnType:
        text = str(arrow)
        for family, matches, describe in self._families():
            if matches(arrow):
                return describe(family, arrow, text)

        return ColumnType(TypeFamily.OTHER, text)

    def _families(self) -> list[tuple[TypeFamily, TypeCheck, TypeDescriber]]:
        return [
            (TypeFamily.INTEGER, pyarrow.types.is_integer, self._width),
            (TypeFamily.FLOAT, pyarrow.types.is_floating, self._width),
            (TypeFamily.DECIMAL, pyarrow.types.is_decimal, self._decimal),
            (TypeFamily.BOOLEAN, pyarrow.types.is_boolean, self._plain),
            (TypeFamily.STRING, pyarrow.types.is_string, self._plain),
            (TypeFamily.STRING, pyarrow.types.is_large_string, self._plain),
            (TypeFamily.BINARY, pyarrow.types.is_binary, self._plain),
            (TypeFamily.BINARY, pyarrow.types.is_large_binary, self._plain),
            (TypeFamily.DATE, pyarrow.types.is_date, self._plain),
            (TypeFamily.TIMESTAMP, pyarrow.types.is_timestamp, self._timestamp),
            (TypeFamily.TIME, pyarrow.types.is_time, self._time),
            (TypeFamily.UUID, self._is_uuid, self._plain),
        ]

    @staticmethod
    def _is_uuid(arrow: pyarrow.DataType) -> bool:
        """Каноническое расширение arrow.uuid: так UUID отдаёт ClickHouse."""
        return isinstance(arrow, pyarrow.UuidType)

    @staticmethod
    def _plain(family: TypeFamily, arrow: pyarrow.DataType, text: str) -> ColumnType:
        return ColumnType(family, text)

    @staticmethod
    def _width(family: TypeFamily, arrow: pyarrow.DataType, text: str) -> ColumnType:
        return ColumnType(
            family,
            text,
            bits=arrow.bit_width,
            unsigned=pyarrow.types.is_unsigned_integer(arrow),
        )

    @staticmethod
    def _decimal(family: TypeFamily, arrow: pyarrow.DataType, text: str) -> ColumnType:
        decimal = cast_decimal(arrow)

        return ColumnType(
            family, text, precision=decimal.precision, scale=decimal.scale
        )

    @staticmethod
    def _timestamp(
        family: TypeFamily, arrow: pyarrow.DataType, text: str
    ) -> ColumnType:
        stamp = cast_timestamp(arrow)

        return ColumnType(
            family, text, unit=TimeUnit(stamp.unit), zoned=stamp.tz is not None
        )

    @staticmethod
    def _time(family: TypeFamily, arrow: pyarrow.DataType, text: str) -> ColumnType:
        clock = cast_time(arrow)

        return ColumnType(family, text, unit=TimeUnit(clock.unit))

    @staticmethod
    def _metadata(field: pyarrow.Field) -> dict[str, str]:
        raw = field.metadata
        if raw is None:
            return {}

        decoded: dict[str, str] = {}
        for key, value in raw.items():
            decoded[key.decode()] = value.decode()

        return decoded

    @staticmethod
    def _length(metadata: dict[str, str]) -> int:
        text = metadata.get(ArrowFieldMeta.CHAR_LENGTH.value)
        if text is None:
            return 0

        return int(text)


def cast_decimal(arrow: pyarrow.DataType) -> pyarrow.Decimal128Type:
    """Тип decimal под своим классом: is_decimal уже проверил."""
    if isinstance(arrow, pyarrow.Decimal128Type | pyarrow.Decimal256Type):
        return arrow

    raise ArrowStreamError(f"decimal type expected, got {arrow}")


def cast_timestamp(arrow: pyarrow.DataType) -> pyarrow.TimestampType:
    if isinstance(arrow, pyarrow.TimestampType):
        return arrow

    raise ArrowStreamError(f"timestamp type expected, got {arrow}")


def cast_time(arrow: pyarrow.DataType) -> pyarrow.Time32Type | pyarrow.Time64Type:
    if isinstance(arrow, pyarrow.Time32Type | pyarrow.Time64Type):
        return arrow

    raise ArrowStreamError(f"time type expected, got {arrow}")
