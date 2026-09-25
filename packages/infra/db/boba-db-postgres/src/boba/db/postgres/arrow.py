"""Postgres потоком Arrow поверх COPY: типы колонок выборки — по описанию
стейтмента у libpq без выполнения (prepare + describe_prepared), сама
выборка уходит сервером как COPY ... TO STDOUT (FORMAT CSV), и читатель CSV
pyarrow собирает пачки Arrow в C по этой схеме; загрузка — пачки Arrow
писателем CSV pyarrow в COPY ... FROM STDIN (FORMAT CSV). Python значений не
касается. Раскладка типов — как у ADBC-драйвера postgres, кроме того, чего
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
import pyarrow.csv
from psycopg import pq, sql
from psycopg._typeinfo import TypeInfo, TypesRegistry
from psycopg.pq.abc import PGresult

from boba.db.postgres.errors import PgArrowError
from boba.toolkit.arrow import ArrowOutbound, ArrowReader

__all__ = ["PgArrowIn", "PgArrowOut", "PgArrowTypes", "PgColumn"]


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

    async def write(self, block: Any) -> None:
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
    3 в опциях соединения."""

    COPY_HEAD: ClassVar[bytes] = b"copy ("
    COPY_TAIL: ClassVar[bytes] = b") to stdout (format csv)"

    def __init__(self, conn: psycopg.AsyncConnection[Any]) -> None:
        self._conn = conn
        self._types = PgArrowTypes(conn.adapters.types)

    async def describe(self, text: str) -> tuple[PgColumn, ...]:
        """Колонки выборки: prepare + describe безымянного стейтмента у libpq,
        стейтмент не выполняется."""
        pgconn = self._conn.pgconn
        try:
            prepared = await asyncio.to_thread(pgconn.prepare, b"", text.encode())
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
                raise PgArrowError(f"column {position} of the statement has no name")

            columns.append(
                PgColumn(
                    name=name.decode(),
                    oid=described.ftype(position),
                    typmod=described.fmod(position),
                )
            )

        return tuple(columns)

    async def stream_into(
        self, text: str, chunk_bytes: int, sink: ArrowOutbound
    ) -> pyarrow.Schema:
        schema = self._types.schema(await self.describe(text))
        writer = await sink.open(schema)
        pipe = CopyPipe()
        reader = CsvBatches(schema, chunk_bytes)

        async def produce() -> None:
            try:
                await self._copy_out(text, pipe, chunk_bytes)
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

        return schema

    async def _copy_out(self, text: str, pipe: CopyPipe, chunk_bytes: int) -> None:
        """COPY отдаёт по блоку на строку: блоки копятся до chunk_bytes и уходят
        в трубу одной записью, иначе каждая строка стоила бы прыжка в поток."""
        statement = self.COPY_HEAD + text.encode() + self.COPY_TAIL
        pending = bytearray()
        async with self._conn.cursor() as cursor, cursor.copy(statement) as copy:
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

    BLOCK_FLOOR: ClassVar[int] = 1 << 20

    def __init__(self, schema: pyarrow.Schema, chunk_bytes: int) -> None:
        self._schema = schema
        self._block = max(chunk_bytes, self.BLOCK_FLOOR)
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

    async def batches(self, source: io.RawIOBase) -> AsyncIterator[Any]:
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

    def _open(self, source: io.RawIOBase) -> Any:
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
    def _next(reader: Any) -> Any:
        try:
            return reader.read_next_batch()
        except StopIteration:
            return None


class PgArrowIn:
    """Пачки Arrow из входного порта в таблицу postgres: писатель CSV pyarrow
    пишет пачку в C, блок уходит в COPY table (cols) FROM STDIN (FORMAT CSV),
    сервер разбирает текст по типу колонки. Одна транзакция. Типы, которых
    CSV не несёт (список, двоичный, вложенные), отвергаются по схеме до
    загрузки: источник отдаёт их текстом."""

    def __init__(self, conn: psycopg.AsyncConnection[Any]) -> None:
        self._conn = conn
        self._options = pyarrow.csv.WriteOptions(include_header=False)

    async def copy_from(self, table: str, reader: ArrowReader) -> int:
        self._ensure_writable(reader.schema)
        names: list[str] = list(reader.schema.names)
        columns = sql.SQL(", ").join(sql.Identifier(name) for name in names)
        statement = sql.SQL("copy {} ({}) from stdin (format csv)").format(
            self._table(table), columns
        )

        rows = 0
        async with (
            self._conn.transaction(),
            self._conn.cursor() as cursor,
            cursor.copy(statement) as copy,
        ):
            async for batch in reader.batches:
                await copy.write(self._csv(batch))
                rows += batch.num_rows

        return rows

    def _csv(self, batch: Any) -> bytes:
        buffer = io.BytesIO()
        try:
            pyarrow.csv.write_csv(batch, buffer, write_options=self._options)
        except pyarrow.ArrowException as exc:
            raise PgArrowError(
                f"writing an arrow batch as csv failed: {type(exc).__name__}: {exc}"
            ) from exc

        return buffer.getvalue()

    @staticmethod
    def _ensure_writable(schema: Any) -> None:
        for field in schema:
            kind = field.type
            if pyarrow.types.is_nested(kind) or pyarrow.types.is_binary(kind):
                raise PgArrowError(
                    f"column {field.name} of type {kind} cannot be written as csv; "
                    f"send it as text from the source (arrays as their text form, "
                    f"binary as hex with the \\x prefix)"
                )

            if pyarrow.types.is_large_binary(kind) or pyarrow.types.is_dictionary(kind):
                raise PgArrowError(
                    f"column {field.name} of type {kind} cannot be written as csv; "
                    f"send it as text from the source"
                )

    @staticmethod
    def _table(table: str) -> sql.Composable:
        """Имя приёмника как в SQL: schema.table или table, каждая часть —
        идентификатор psycopg."""
        parts = table.split(".")
        return sql.SQL(".").join(sql.Identifier(part) for part in parts)
