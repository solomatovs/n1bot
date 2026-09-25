"""Postgres потоком Arrow поверх COPY: типы колонок выборки — по описанию
стейтмента у libpq без выполнения (prepare + describe_prepared), сама
выборка уходит сервером как COPY ... TO STDOUT (FORMAT CSV), и читатель CSV
pyarrow собирает пачки Arrow в C по этой схеме; загрузка — пачки Arrow
писателем CSV pyarrow в стейтмент COPY ... FROM STDIN (FORMAT CSV), который
пишет вызывающий. Python значений не касается. Раскладка типов — как у
ADBC-драйвера postgres, кроме того, чего
CSV не несёт: bytea и массивы едут текстом сервера, numeric без точности
отвергается до выполнения.

Ошибки:
PgArrowError — стейтмент не описывается сервером, колонка выборки не
    укладывается в Arrow (numeric без точности) или тип пачки на загрузке
    не пишется в CSV (список, двоичный, вложенный).
psycopg.Error — сервер отклонил стейтмент или значение при загрузке.
"""

from __future__ import annotations

import asyncio
import io
import os
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, ClassVar

import psycopg
import pyarrow
import pyarrow.compute
import pyarrow.csv
from psycopg import pq, sql
from psycopg._typeinfo import TypeInfo, TypesRegistry
from psycopg.pq.abc import PGresult

from boba.db.postgres.errors import PgArrowError
from boba.db.postgres.query import PgQueryBuilder
from boba.db.postgres.trace import PgCommandReport, PgSessionTrace
from boba.toolkit.arrow import ArrowIpc, ArrowReader
from boba.toolkit.ports import ArrowOutbound

__all__ = [
    "Compute",
    "HexFloats",
    "PgArrowIn",
    "PgArrowOut",
    "PgArrowTypes",
    "PgColumn",
]


@dataclass(frozen=True)
class PgColumn:
    """Колонка выборки по описанию libpq: имя, OID типа и typmod."""

    name: str
    oid: int
    typmod: int


class PgType(StrEnum):
    """Имена типов postgres из реестра psycopg, у которых есть родной тип Arrow
    и которые читатель CSV разбирает сам; всё остальное едет текстом."""

    INT2 = "int2"
    INT4 = "int4"
    INT8 = "int8"
    OID = "oid"
    FLOAT4 = "float4"
    FLOAT8 = "float8"
    BOOL = "bool"
    NUMERIC = "numeric"
    DATE = "date"
    TIMESTAMP = "timestamp"
    TIMESTAMPTZ = "timestamptz"


class CsvText(StrEnum):
    """Как postgres пишет CSV: истина и ложь у boolean, NULL — пустое поле."""

    TRUE = "t"
    FALSE = "f"
    NULL = ""


class PgArrowTypes:
    """Тип Arrow для колонки postgres по OID и typmod из реестра типов psycopg:
    числа, boolean, даты и время — родными типами, numeric(p, s) до 38 знаков
    — decimal128, всё остальное (text, bytea в hex, массивы, uuid, json,
    inet, interval, enum, составные) — large_string текстом сервера. numeric
    без точности — отказ: масштаб значений неизвестен; шире 38 знаков —
    отказ: читатель CSV decimal256 не собирает."""

    VARHDRSZ: ClassVar[int] = 4
    DECIMAL128_DIGITS: ClassVar[int] = 38
    UNBOUNDED: ClassVar[int] = -1
    ARROW: ClassVar[Mapping[str, pyarrow.DataType]] = {
        PgType.INT2: pyarrow.int16(),
        PgType.INT4: pyarrow.int32(),
        PgType.INT8: pyarrow.int64(),
        PgType.OID: pyarrow.uint32(),
        PgType.FLOAT4: pyarrow.float32(),
        PgType.FLOAT8: pyarrow.float64(),
        PgType.BOOL: pyarrow.bool_(),
        PgType.DATE: pyarrow.date32(),
        PgType.TIMESTAMP: pyarrow.timestamp("us"),
        PgType.TIMESTAMPTZ: pyarrow.timestamp("us", "UTC"),
    }

    def __init__(self, registry: TypesRegistry) -> None:
        self._registry = registry

    def schema(self, columns: Sequence[PgColumn]) -> pyarrow.Schema:
        fields: list[pyarrow.Field] = []
        for column in columns:
            fields.append(pyarrow.field(column.name, self.of(column)))

        return pyarrow.schema(fields)

    def of(self, column: PgColumn) -> pyarrow.DataType:
        info: TypeInfo | None = self._registry.get(column.oid)
        if info is None:
            return pyarrow.large_string()

        if info.array_oid == column.oid:
            return pyarrow.large_string()

        if info.name == PgType.NUMERIC:
            return self._numeric(column.name, column.typmod)

        arrow = self.ARROW.get(info.name)
        if arrow is not None:
            return arrow

        return pyarrow.large_string()

    def _numeric(self, name: str, typmod: int) -> pyarrow.DataType:
        if typmod == self.UNBOUNDED:
            raise PgArrowError(
                f"column {name} is numeric without precision: the scale of its "
                f"values is unknown, cast it in the select: {name}::numeric(p, s) "
                f"or {name}::text"
            )

        packed = typmod - self.VARHDRSZ
        precision = packed >> 16
        scale = packed & 0xFFFF
        if precision <= self.DECIMAL128_DIGITS:
            return pyarrow.decimal128(precision, scale)

        raise PgArrowError(
            f"column {name} is numeric({precision}, {scale}): the csv reader of "
            f"arrow holds decimals up to {self.DECIMAL128_DIGITS} digits, cast it "
            f"in the select: {name}::text"
        )


class CopyPipe:
    """Труба между COPY и читателем CSV: async-цикл COPY пишет блоки в конец
    записи в потоке, читатель pyarrow читает конец чтения в своём потоке.
    Оба конца закрывает владелец, когда его сторона завершилась."""

    def __init__(self) -> None:
        self._read_fd, self._write_fd = os.pipe()
        self.source = os.fdopen(self._read_fd, "rb", buffering=0)

    async def write(self, block: bytes | bytearray | memoryview) -> None:
        await asyncio.to_thread(self._write_all, memoryview(block))

    def close_write(self) -> None:
        os.close(self._write_fd)

    def close_read(self) -> None:
        self.source.close()

    def _write_all(self, block: memoryview) -> None:
        written = 0
        while written < len(block):
            written += os.write(self._write_fd, block[written:])


class PgArrowOut:
    """Выборка postgres потоком Arrow в выходной порт: описание стейтмента у
    libpq без выполнения даёт схему, затем сервер отдаёт COPY (стейтмент) TO
    STDOUT (FORMAT CSV), а читатель CSV pyarrow собирает пачки Arrow по этой
    схеме блоками chunk_bytes. float печатается точно при extra_float_digits =
    3 в опциях соединения. Notices и notify сессии попадают в итог."""

    def __init__(self, conn: psycopg.AsyncConnection[Any]) -> None:
        self._conn = conn
        self._types = PgArrowTypes(conn.adapters.types)
        self._trace = PgSessionTrace(conn)
        self._ipc = ArrowIpc()

    async def describe(self, text: str) -> Sequence[PgColumn]:
        """Колонки выборки по описанию безымянного стейтмента у libpq: сервер
        разбирает и планирует запрос, но не выполняет его."""
        pgconn = self._conn.pgconn
        encoding = self._conn.info.encoding
        try:
            prepared = await asyncio.to_thread(
                pgconn.prepare, b"", text.encode(encoding)
            )
            self._ensure_ok(prepared, "preparing", text)
            described = await asyncio.to_thread(pgconn.describe_prepared, b"")
            self._ensure_ok(described, "describing", text)
        except psycopg.Error as exc:
            raise PgArrowError(
                f"describing the statement on postgres failed: {type(exc).__name__}: "
                f"{exc}; query: {text[:200]!r}"
            ) from exc

        columns: list[PgColumn] = []
        for position in range(described.nfields):
            name = described.fname(position)
            if name is None:
                raise PgArrowError(
                    f"describing the statement on postgres: column {position} has "
                    f"no name; query: {text[:200]!r}"
                )

            columns.append(
                PgColumn(
                    name=name.decode(encoding),
                    oid=described.ftype(position),
                    typmod=described.fmod(position),
                )
            )

        return tuple(columns)

    async def stream_into(
        self, text: str, chunk_bytes: int, sink: ArrowOutbound
    ) -> PgCommandReport:
        schema = self._types.schema(await self.describe(text))
        writer = await self._ipc.open_out(sink, schema)
        pipe = CopyPipe()
        reader = CsvBatches(schema, chunk_bytes)
        query = (
            PgQueryBuilder()
            .add("copy (")
            .raw_query(text)
            .add(") to stdout (format csv)")
            .build()
        )

        async with self._conn.cursor() as cursor:

            async def produce() -> None:
                try:
                    await self._copy_out(cursor, query.text, pipe, chunk_bytes)
                finally:
                    pipe.close_write()

            async def consume() -> None:
                try:
                    async for batch in reader.batches(pipe.source):
                        await writer.write(batch)
                finally:
                    pipe.close_read()

            await asyncio.gather(produce(), consume())
            await writer.close()

            return self._trace.report(
                f"streamed out arrow ipc: {', '.join(schema.names)}",
                query.text.as_string(self._conn),
                cursor,
            )

    async def _copy_out(
        self,
        cursor: psycopg.AsyncCursor[Any],
        statement: sql.Composed,
        pipe: CopyPipe,
        chunk_bytes: int,
    ) -> None:
        """COPY отдаёт по блоку на строку: блоки копятся до chunk_bytes и уходят
        в трубу одной записью, иначе каждая строка стоила бы прыжка в поток."""
        pending = bytearray()
        async with cursor.copy(statement) as copy:
            async for block in copy:
                pending.extend(block)
                if len(pending) < chunk_bytes:
                    continue

                await pipe.write(pending)
                pending = bytearray()

        if pending:
            await pipe.write(pending)

    @staticmethod
    def _ensure_ok(result: PGresult, action: str, text: str) -> None:
        if result.status == pq.ExecStatus.COMMAND_OK:
            return

        message = result.error_message.decode(errors="replace").strip()
        raise PgArrowError(
            f"{action} the statement on postgres failed: {message}; "
            f"query: {text[:200]!r}"
        )


class CsvBatches:
    """Пачки Arrow из CSV postgres по явной схеме: читатель pyarrow разбирает
    поля в C, блок на пачку — chunk_bytes байт, но не меньше BLOCK_FLOOR:
    строка CSV обязана уместиться в один блок, иначе читатель отказывает;
    `t`/`f` — boolean, пустое поле без кавычек — NULL, в кавычках — пустая
    строка, переводы строк внутри кавычек допустимы."""

    BLOCK_SIZE: ClassVar[int] = 1 << 20

    def __init__(self, schema: pyarrow.Schema, chunk_bytes: int) -> None:
        self._schema = schema
        self._block = max(chunk_bytes, self.BLOCK_SIZE)
        self._read = pyarrow.csv.ReadOptions(
            column_names=schema.names, block_size=self._block
        )
        self._parse = pyarrow.csv.ParseOptions(newlines_in_values=True)
        self._convert = pyarrow.csv.ConvertOptions(
            column_types=schema,
            true_values=[CsvText.TRUE.value],
            false_values=[CsvText.FALSE.value],
            null_values=[CsvText.NULL.value],
            strings_can_be_null=True,
            quoted_strings_can_be_null=False,
        )

    async def batches(self, source: io.RawIOBase) -> AsyncIterator[pyarrow.RecordBatch]:
        try:
            reader = await asyncio.to_thread(self._open, source)
        except pyarrow.ArrowException as exc:
            raise PgArrowError(
                f"reading the copy csv stream as arrow failed at the first block "
                f"of {self._block} bytes (a row must fit into one block, raise "
                f"chunk_bytes for wider rows): {type(exc).__name__}: {exc}"
            ) from exc

        while True:
            try:
                batch = await asyncio.to_thread(self._next, reader)
            except pyarrow.ArrowException as exc:
                raise PgArrowError(
                    f"reading the copy csv stream as arrow failed: "
                    f"{type(exc).__name__}: {exc}"
                ) from exc

            if batch is None:
                return

            yield batch

    def _open(self, source: io.RawIOBase) -> pyarrow.csv.CSVStreamingReader:
        # pyarrow берёт блоком то, что вернул один read: труба отдаёт короткие
        # чтения, а BufferedReader добирает до полного блока или конца потока
        filled = io.BufferedReader(source, self._block)

        return pyarrow.csv.open_csv(
            filled,
            read_options=self._read,
            parse_options=self._parse,
            convert_options=self._convert,
        )

    @staticmethod
    def _next(reader: pyarrow.csv.CSVStreamingReader) -> pyarrow.RecordBatch | None:
        try:
            return reader.read_next_batch()
        except StopIteration:
            return None


class Compute(StrEnum):
    """Функции pyarrow.compute, из которых собирается hex-запись: они
    регистрируются в реестре pyarrow при загрузке и зовутся по имени."""

    LESS = "less"
    EQUAL = "equal"
    GREATER_EQUAL = "greater_equal"
    AND = "and"
    BIT_AND = "bit_wise_and"
    SHIFT_RIGHT = "shift_right"
    SUBTRACT = "subtract"
    IF_ELSE = "if_else"
    IS_FINITE = "is_finite"
    JOIN = "binary_join_element_wise"


class HexFloats:
    """Колонки float и double пачки шестнадцатеричным текстом C99 (`0x1.8p+3`):
    strtod libc разбирает такую запись бит в бит на любой версии postgres,
    тогда как десятичную запись Greenplum 6 для части значений округляет на
    одну ULP. Всё считается pyarrow.compute по битам значения, Python до
    значений не доходит. NaN и бесконечности остаются текстом pyarrow."""

    MANTISSA_BITS: ClassVar[int] = 52
    EXPONENT_MASK: ClassVar[int] = 0x7FF
    EXPONENT_BIAS: ClassVar[int] = 1023
    NIBBLES: ClassVar[int] = 13

    def __init__(self) -> None:
        self._digits = pyarrow.array(list("0123456789abcdef"), pyarrow.string())

    def render(self, batch: pyarrow.RecordBatch) -> pyarrow.RecordBatch:
        for position, column in enumerate(batch.schema):
            if not pyarrow.types.is_floating(column.type):
                continue

            hexed = self.column(batch.column(position))
            batch = batch.set_column(
                position, pyarrow.field(column.name, hexed.type), hexed
            )

        return batch

    def column(self, values: pyarrow.Array) -> pyarrow.Array:
        doubles = values.cast(pyarrow.float64())
        bits = doubles.view(pyarrow.uint64())
        negative = self._call(Compute.LESS, doubles.view(pyarrow.int64()), 0)
        shifted = self._call(Compute.SHIFT_RIGHT, bits, self._u64(self.MANTISSA_BITS))
        exponent = pyarrow.compute.cast(
            self._call(Compute.BIT_AND, shifted, self._u64(self.EXPONENT_MASK)),
            pyarrow.int64(),
        )
        mantissa = self._call(
            Compute.BIT_AND, bits, self._u64((1 << self.MANTISSA_BITS) - 1)
        )

        nibbles: list[pyarrow.Array] = []
        for position in range(self.NIBBLES):
            shift = self._u64(4 * (self.NIBBLES - 1 - position))
            nibble = self._call(
                Compute.BIT_AND,
                self._call(Compute.SHIFT_RIGHT, mantissa, shift),
                self._u64(0xF),
            )
            nibbles.append(pyarrow.compute.take(self._digits, nibble))

        subnormal = self._call(Compute.EQUAL, exponent, 0)
        zero = self._call(
            Compute.AND, subnormal, self._call(Compute.EQUAL, mantissa, self._u64(0))
        )
        lead = self._call(Compute.IF_ELSE, subnormal, "0x0.", "0x1.")
        power = self._call(
            Compute.IF_ELSE,
            subnormal,
            self._call(Compute.SUBTRACT, exponent, self.EXPONENT_BIAS - 1),
            self._call(Compute.SUBTRACT, exponent, self.EXPONENT_BIAS),
        )
        power = self._call(Compute.IF_ELSE, zero, 0, power)
        positive = self._call(Compute.GREATER_EQUAL, power, 0)
        marker = self._call(Compute.IF_ELSE, positive, "p+", "p")
        sign = self._call(Compute.IF_ELSE, negative, "-", "")
        text = self._call(
            Compute.JOIN,
            sign,
            lead,
            *nibbles,
            marker,
            pyarrow.compute.cast(power, pyarrow.string()),
            "",
        )
        finite = self._call(Compute.IS_FINITE, doubles)
        fallback = pyarrow.compute.cast(doubles, pyarrow.string())

        return self._call(Compute.IF_ELSE, finite, text, fallback)

    @staticmethod
    def _call(function: Compute, *args: object) -> pyarrow.Array:
        return pyarrow.compute.call_function(function.value, list(args))

    @staticmethod
    def _u64(value: int) -> pyarrow.Scalar:
        return pyarrow.scalar(value, pyarrow.uint64())


class PgArrowIn:
    """Пачки Arrow из входного порта в стейтмент COPY ... FROM STDIN (FORMAT
    CSV), который написал вызывающий: писатель CSV pyarrow пишет пачку в C,
    блок уходит в COPY, сервер разбирает текст по типу колонки; колонки
    стейтмента идут в порядке полей потока. Одна транзакция. Типы, которых
    CSV не несёт (список, двоичный, вложенные), отвергаются по схеме до
    загрузки: источник отдаёт их текстом. С exact_floats float и double
    едут hex-записью (HexFloats) и ложатся бит в бит на любом сервере."""

    def __init__(self, conn: psycopg.AsyncConnection[Any], exact_floats: bool) -> None:
        self._conn = conn
        self._exact_floats = exact_floats
        self._hex = HexFloats()
        self._trace = PgSessionTrace(conn)
        self._options = pyarrow.csv.WriteOptions(include_header=False)

    async def copy_from(self, statement: str, reader: ArrowReader) -> PgCommandReport:
        self._ensure_writable(reader.schema)
        query = PgQueryBuilder().raw_query(statement).build()

        rows = 0
        async with self._conn.transaction(), self._conn.cursor() as cursor:
            async with cursor.copy(query.text) as copy:
                async for batch in reader.batches:
                    await copy.write(self._csv(batch))
                    rows += batch.num_rows

            return self._trace.report(f"{rows} rows written", statement, cursor)

    def _csv(self, batch: pyarrow.RecordBatch) -> bytes:
        if self._exact_floats:
            batch = self._hex.render(batch)

        buffer = io.BytesIO()
        try:
            pyarrow.csv.write_csv(batch, buffer, write_options=self._options)
        except pyarrow.ArrowException as exc:
            raise PgArrowError(
                f"writing an arrow batch as csv failed: {type(exc).__name__}: {exc}"
            ) from exc

        return buffer.getvalue()

    @staticmethod
    def _ensure_writable(schema: pyarrow.Schema) -> None:
        for column in schema:
            kind = column.type
            if pyarrow.types.is_nested(kind) or pyarrow.types.is_binary(kind):
                raise PgArrowError(
                    f"column {column.name} of type {kind} cannot be written as csv; "
                    f"send it as text from the source (arrays as their text form, "
                    f"binary as hex with the \\x prefix)"
                )

            if pyarrow.types.is_large_binary(kind) or pyarrow.types.is_dictionary(kind):
                raise PgArrowError(
                    f"column {column.name} of type {kind} cannot be written as csv; "
                    f"send it as text from the source"
                )
