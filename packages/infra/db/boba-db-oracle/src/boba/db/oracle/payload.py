"""Oracle для payload'ов и скраперов: thin-соединение python-oracledb по профилю,
строки запроса потоком с именованными bind'ами, CSV-байтами пачками Arrow
(блоками или прямо в файл) или потоком Arrow IPC прямо в файл; запись пачками
через executemany — строками или пачками Arrow из входного потока IPC.

Ошибки:
OracleQueryError — сервер отклонил запрос или оборвал чтение (в том числе по
    call_timeout).
OracleError — до базы не достучаться: сеть, listener, отказ при входе.
OracleFormatError — входной поток не читается как Arrow IPC.
"""

from __future__ import annotations

import asyncio
import io
from collections.abc import AsyncGenerator, AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, BinaryIO, ClassVar

import oracledb
import pyarrow
import pyarrow.csv
import pyarrow.ipc
from oracledb import (
    DB_TYPE_BINARY_DOUBLE,
    DB_TYPE_BINARY_FLOAT,
    DB_TYPE_BINARY_INTEGER,
    DB_TYPE_BLOB,
    DB_TYPE_BOOLEAN,
    DB_TYPE_CHAR,
    DB_TYPE_CLOB,
    DB_TYPE_DATE,
    DB_TYPE_LONG,
    DB_TYPE_LONG_NVARCHAR,
    DB_TYPE_LONG_RAW,
    DB_TYPE_NCHAR,
    DB_TYPE_NCLOB,
    DB_TYPE_NUMBER,
    DB_TYPE_NVARCHAR,
    DB_TYPE_RAW,
    DB_TYPE_TIMESTAMP,
    DB_TYPE_TIMESTAMP_LTZ,
    DB_TYPE_TIMESTAMP_TZ,
    DB_TYPE_VARCHAR,
    AsyncConnection,
    AsyncCursor,
    DbType,
    FetchInfo,
)

from boba.db.oracle.connection import OracleConfig
from boba.db.oracle.errors import OracleError, OracleFormatError, OracleQueryError

__all__ = [
    "ArrowInbound",
    "ArrowTypes",
    "ByteStream",
    "OraColumnType",
    "PayloadOracle",
    "RowStream",
]


class OraColumnType(StrEnum):
    """Семейство типа колонки глазами загрузчика текста: как привести строку CSV
    к значению bind'а. Точный тип драйвера наружу не выходит."""

    NUMBER = "number"
    FLOAT = "float"
    DATE = "date"
    TIMESTAMP = "timestamp"
    BINARY = "binary"
    TEXT = "text"

    @classmethod
    def of(cls, db_type: DbType) -> OraColumnType:
        if db_type in (DB_TYPE_NUMBER, DB_TYPE_BINARY_INTEGER):
            return cls.NUMBER

        if db_type in (DB_TYPE_BINARY_DOUBLE, DB_TYPE_BINARY_FLOAT):
            return cls.FLOAT

        if db_type is DB_TYPE_DATE:
            return cls.DATE

        if db_type in (DB_TYPE_TIMESTAMP, DB_TYPE_TIMESTAMP_TZ, DB_TYPE_TIMESTAMP_LTZ):
            return cls.TIMESTAMP

        if db_type in (DB_TYPE_RAW, DB_TYPE_LONG_RAW, DB_TYPE_BLOB):
            return cls.BINARY

        return cls.TEXT


@dataclass(frozen=True, slots=True)
class RowStream:
    """Строки одного запроса: имена колонок и сами строки асинхронным потоком.

    Драйвер отдаёт имена колонок отдельно от значений, поэтому они едут вместе
    с потоком: вызывающий собирает словарь строки по names, не заглядывая во
    внутренности курсора. Имена в нижнем регистре: Oracle хранит их заглавными.
    У команды без выборки (DML, DDL, PL/SQL) names пуст, а affected — число
    затронутых строк; итерировать blocks такой команды нельзя.
    """

    names: tuple[str, ...]
    blocks: AsyncIterator[Sequence[Any]]
    affected: int = 0


@dataclass(frozen=True)
class ByteStream:
    """Ответ запроса байтами CSV без заголовка: имена колонок и блоки по пачкам
    Arrow в arraysize строк."""

    names: tuple[str, ...]
    blocks: AsyncIterator[memoryview]


@dataclass(frozen=True)
class ArrowInbound:
    """Входной поток Arrow IPC: схема из его начала и пачки по мере чтения."""

    schema: pyarrow.Schema
    batches: AsyncIterator[pyarrow.RecordBatch]


class ArrowTypes:
    """Тип Arrow для колонки по описанию курсора после parse — та же раскладка,
    что драйвер выбирает сам при fetch_df, кроме NUMBER: без точности он отдал
    бы double, поэтому NUMBER(p, s) запрашивается decimal128(p, s), без точности
    — decimal128(38, 0), NUMBER(p, -s) — decimal128(p + s, 0), FLOAT(p) —
    double. Типы, которые драйвер в Arrow не отдаёт (INTERVAL, XMLTYPE, JSON,
    ROWID, VECTOR, объекты), отвергаются до выполнения запроса с подсказкой,
    чем их привести в самом select."""

    # NUMBER без объявленной точности: столько знаков вмещает decimal128
    UNBOUNDED_NUMBER: ClassVar[pyarrow.DataType] = pyarrow.decimal128(38, 0)
    # масштаб, которым драйвер помечает FLOAT(p) и NUMBER без точности
    FLOAT_SCALE: ClassVar[int] = -127
    # доли секунды, до которых Arrow-пачка драйвера хранит микросекунды
    MICROSECONDS: ClassVar[int] = 6

    TEXT: ClassVar[frozenset[DbType]] = frozenset(
        {
            DB_TYPE_VARCHAR,
            DB_TYPE_NVARCHAR,
            DB_TYPE_CHAR,
            DB_TYPE_NCHAR,
            DB_TYPE_CLOB,
            DB_TYPE_NCLOB,
            DB_TYPE_LONG,
            DB_TYPE_LONG_NVARCHAR,
        }
    )
    BINARY: ClassVar[frozenset[DbType]] = frozenset(
        {DB_TYPE_RAW, DB_TYPE_LONG_RAW, DB_TYPE_BLOB}
    )
    TIMESTAMPS: ClassVar[frozenset[DbType]] = frozenset(
        {DB_TYPE_TIMESTAMP, DB_TYPE_TIMESTAMP_TZ, DB_TYPE_TIMESTAMP_LTZ}
    )
    FIXED: ClassVar[Mapping[DbType, pyarrow.DataType]] = {
        DB_TYPE_BINARY_INTEGER: pyarrow.int64(),
        DB_TYPE_BINARY_DOUBLE: pyarrow.float64(),
        DB_TYPE_BINARY_FLOAT: pyarrow.float32(),
        DB_TYPE_DATE: pyarrow.timestamp("s"),
        DB_TYPE_BOOLEAN: pyarrow.bool_(),
    }
    HINTS: ClassVar[Mapping[str, str]] = {
        "DB_TYPE_INTERVAL_YM": "months as a number: extract(year from col) * 12 "
        "+ extract(month from col), or to_char(col)",
        "DB_TYPE_INTERVAL_DS": "seconds as a number: extract(day from col) * 86400 "
        "+ ... + extract(second from col), or to_char(col)",
        "DB_TYPE_XMLTYPE": "xmlserialize(document col as clob)",
        "DB_TYPE_JSON": "json_serialize(col returning clob)",
        "DB_TYPE_ROWID": "rowidtochar(col)",
        "DB_TYPE_UROWID": "rowidtochar(col)",
        "DB_TYPE_VECTOR": "from_vector(col)",
    }

    def schema(self, described: Sequence[FetchInfo]) -> pyarrow.Schema:
        fields: list[pyarrow.Field] = []
        for column in described:
            fields.append(pyarrow.field(column.name, self.of(column)))

        return pyarrow.schema(fields)

    def of(self, column: FetchInfo) -> pyarrow.DataType:
        kind = column.type
        if kind is DB_TYPE_NUMBER:
            return self._number(column)

        if kind in self.TIMESTAMPS:
            return self._timestamp(column)

        if kind in self.TEXT:
            return pyarrow.large_string()

        if kind in self.BINARY:
            return pyarrow.large_binary()

        fixed = self.FIXED.get(kind)
        if fixed is not None:
            return fixed

        hint = self.HINTS.get(kind.name, "a text or number expression")
        raise OracleQueryError(
            f"column {column.name} of type {kind.name} cannot be fetched as arrow "
            f"by the driver; convert it in the select: {hint}"
        )

    def _number(self, column: FetchInfo) -> pyarrow.DataType:
        precision = column.precision
        scale = column.scale
        if precision is None or scale is None:
            return self.UNBOUNDED_NUMBER

        if precision == 0:
            return self.UNBOUNDED_NUMBER

        if scale == self.FLOAT_SCALE:
            return pyarrow.float64()

        if scale < 0:
            return pyarrow.decimal128(precision - scale, 0)

        return pyarrow.decimal128(precision, scale)

    def _timestamp(self, column: FetchInfo) -> pyarrow.DataType:
        fraction = column.scale
        if fraction is None:
            return pyarrow.timestamp("us")

        if fraction == 0:
            return pyarrow.timestamp("s")

        if fraction <= self.MICROSECONDS:
            return pyarrow.timestamp("us")

        return pyarrow.timestamp("ns")


class PayloadOracle:
    """Соединение по профилю и запросы на нём: строки потоком или CSV-байты пачками.

    Создаётся на профиль OracleConfig; из него берутся параметры соединения,
    call_timeout и arraysize курсоров. Пула нет: скрапер держит одно соединение на
    попытку, payload — на вызов. LOB-колонки читаются строками и байтами, а не
    объектами LOB, чтобы поток строк не зависел от открытого курсора; NUMBER
    приходит Decimal, а не float: битовые поля словаря шире 2^53.
    """

    def __init__(self, connection: OracleConfig) -> None:
        self._connection = connection
        self._types = ArrowTypes()

    @asynccontextmanager
    async def opened(self) -> AsyncGenerator[AsyncConnection, None]:
        """Соединение на время операции; закрывается на выходе из блока."""
        oracledb.defaults.fetch_lobs = False
        oracledb.defaults.fetch_decimals = True

        connection = self._connection
        try:
            conn = await oracledb.connect_async(**connection.connect_settings())
        except oracledb.Error as exc:
            raise OracleError(
                f"connecting to oracle {connection.address_prefix()} "
                f"as {connection.trace()}: {type(exc).__name__}: {exc}"
            ) from exc

        conn.call_timeout = connection.call_timeout
        try:
            yield conn
        finally:
            await conn.close()

    @asynccontextmanager
    async def rows(
        self,
        conn: AsyncConnection,
        text: str,
        parameters: Mapping[str, object] | None = None,
    ) -> AsyncGenerator[RowStream, None]:
        """Строки запроса на открытом соединении: одна сессия на много запросов.
        Именованные bind'ы `:name`, только скалярные значения: коллекции thin-режим
        читает не в каждой кодировке базы."""
        binds: dict[str, Any] = {}
        if parameters:
            binds = dict(parameters)

        cursor = await self._executed(conn, text, binds)
        try:
            yield RowStream(
                names=self._names(cursor),
                blocks=self._iterate(cursor, text),
                affected=cursor.rowcount,
            )
        finally:
            cursor.close()

    async def column_types(
        self, conn: AsyncConnection, text: str
    ) -> tuple[OraColumnType, ...]:
        """Семейства типов колонок запроса по описанию после parse: сервер
        разбирает стейтмент и не выполняет его."""
        kinds: list[OraColumnType] = []
        for column in await self._described(conn, text):
            kinds.append(OraColumnType.of(column.type))

        return tuple(kinds)

    async def executemany(
        self,
        conn: AsyncConnection,
        text: str,
        kinds: Sequence[OraColumnType],
        rows: Sequence[Sequence[object]],
    ) -> int:
        """Одна команда для пачки строк: позиционные bind'ы `:1..:n` по семействам
        kinds, драйвер шлёт пачку серверу одной поездкой. Семейство задаёт тип
        bind'а: без него datetime уехал бы как DATE и потерял доли секунды.
        Возвращает число затронутых строк; транзакцию завершает вызывающий."""
        cursor = conn.cursor()
        try:
            cursor.setinputsizes(*self._bind_types(kinds))
            await cursor.executemany(text, list(rows))
            affected = cursor.rowcount
        except oracledb.Error as exc:
            raise OracleQueryError(
                f"executemany on oracle failed for {len(rows)} rows: "
                f"{type(exc).__name__}: {exc}; statement: {text[:200]!r}"
            ) from exc
        finally:
            cursor.close()

        return affected

    @staticmethod
    def _bind_types(kinds: Sequence[OraColumnType]) -> list[DbType | None]:
        """Тип bind'а по семейству: у TIMESTAMP и BINARY он явный, остальные
        драйвер выводит из значения."""
        types: list[DbType | None] = []
        for kind in kinds:
            if kind is OraColumnType.TIMESTAMP:
                types.append(DB_TYPE_TIMESTAMP)
                continue

            if kind is OraColumnType.BINARY:
                types.append(DB_TYPE_RAW)
                continue

            types.append(None)

        return types

    async def commit(self, conn: AsyncConnection) -> None:
        try:
            await conn.commit()
        except oracledb.Error as exc:
            raise OracleQueryError(
                f"commit on oracle failed: {type(exc).__name__}: {exc}"
            ) from exc

    @asynccontextmanager
    async def csv(
        self, conn: AsyncConnection, text: str
    ) -> AsyncGenerator[ByteStream, None]:
        """Ответ запроса CSV-байтами без заголовка пачками Arrow по arraysize строк:
        драйвер декодирует ответ Oracle в массивы Arrow в Cython, pyarrow пишет CSV
        в C, Python делает один шаг на пачку. NULL это пустое поле, строка с
        кавычкой, запятой или переводом строки — в кавычках, DATE и TIMESTAMP — ISO
        с пробелом. Типы колонок — по описанию стейтмента после parse, без
        выполнения (ArrowTypes): NUMBER без точности — decimal128(38, 0), дробь
        в такой колонке — ошибка DPY-4042, её запрос обязан привести сам
        (to_char или number(p, s)). RAW запрос отдаёт `rawtohex`: bytes в CSV
        не пишутся."""
        schema = await self._requested_schema(conn, text)

        yield ByteStream(
            names=self._schema_names(schema),
            blocks=self._csv_batches(conn, text, schema),
        )

    async def _requested_schema(
        self, conn: AsyncConnection, text: str
    ) -> pyarrow.Schema:
        """Схема ответа по описанию колонок после parse — запрос не выполняется."""
        return self._types.schema(await self._described(conn, text))

    async def _described(
        self, conn: AsyncConnection, text: str
    ) -> tuple[FetchInfo, ...]:
        """Колонки стейтмента: parse на сервере без выполнения. На время parse
        кэш стейтментов соединения выключен: разобранный стейтмент в кэше
        ломает последующую Arrow-выборку того же текста (DPY-5002 в
        python-oracledb 26)."""
        cache_size = conn.stmtcachesize
        conn.stmtcachesize = 0
        cursor = conn.cursor()
        try:
            await cursor.parse(text)
            described = tuple(cursor.description or ())
        except oracledb.Error as exc:
            raise OracleQueryError(
                f"parsing the statement on oracle failed: {type(exc).__name__}: "
                f"{exc}; query: {text[:200]!r}"
            ) from exc
        finally:
            cursor.close()
            conn.stmtcachesize = cache_size

        return described

    @staticmethod
    def _schema_names(schema: pyarrow.Schema) -> tuple[str, ...]:
        names: list[str] = []
        for name in schema.names:
            names.append(str(name).lower())

        return tuple(names)

    async def _executed(
        self, conn: AsyncConnection, text: str, binds: Mapping[str, Any]
    ) -> AsyncCursor:
        cursor = conn.cursor()
        cursor.arraysize = self._connection.arraysize

        try:
            await cursor.execute(text, dict(binds))
        except oracledb.Error as exc:
            cursor.close()
            raise OracleQueryError(
                f"query on oracle failed: {type(exc).__name__}: {exc}; "
                f"query: {text[:200]!r}"
            ) from exc

        return cursor

    @staticmethod
    def _names(cursor: AsyncCursor) -> tuple[str, ...]:
        names: list[str] = []
        for column in cursor.description or ():
            names.append(str(column[0]).lower())

        return tuple(names)

    async def arrow_into(
        self, conn: AsyncConnection, text: str, sink: BinaryIO
    ) -> pyarrow.Schema:
        """Ответ запроса потоком Arrow IPC прямо в двоичный файл (сырой порт):
        схема, затем пачки драйвера по arraysize строк как есть, без перевода
        в текст. Схема та же, что у csv: NUMBER без точности — decimal128(38, 0),
        NUMBER(p, -s) — decimal128(p + s, 0); типы, которые драйвер в Arrow не
        отдаёт (INTERVAL, XMLTYPE, JSON, VECTOR, ROWID), запрос приводит сам.
        Запись в sink блокирующая и идёт в потоке."""
        schema = await self._requested_schema(conn, text)
        writer = await asyncio.to_thread(pyarrow.ipc.new_stream, sink, schema)
        async for table in self._tables(conn, text, schema):
            try:
                await asyncio.to_thread(writer.write_table, table)
            except pyarrow.ArrowException as exc:
                raise OracleQueryError(
                    f"writing an arrow batch as ipc failed, the batch schema "
                    f"{table.schema} differs from {schema}: {type(exc).__name__}: "
                    f"{exc}; query: {text[:200]!r}"
                ) from exc

        await asyncio.to_thread(writer.close)

        return schema

    async def csv_into(
        self, conn: AsyncConnection, text: str, sink: BinaryIO
    ) -> tuple[str, ...]:
        """Ответ запроса CSV-байтами без заголовка прямо в двоичный файл (сырой
        порт): pyarrow пишет каждую пачку в sink сам, без промежуточного
        буфера. Правила формата — как у csv. Запись блокирующая и идёт в
        потоке."""
        schema = await self._requested_schema(conn, text)
        options = pyarrow.csv.WriteOptions(include_header=False)
        async for table in self._tables(conn, text, schema):
            try:
                await asyncio.to_thread(
                    pyarrow.csv.write_csv, table, sink, write_options=options
                )
            except pyarrow.ArrowException as exc:
                raise OracleQueryError(
                    f"writing an arrow batch as csv failed, every column must be "
                    f"text, number or date (binary needs rawtohex): "
                    f"{type(exc).__name__}: {exc}; query: {text[:200]!r}"
                ) from exc

        return self._schema_names(schema)

    async def arrow_inbound(
        self, source: io.RawIOBase, buffer_bytes: int
    ) -> ArrowInbound:
        """Входной поток Arrow IPC из сырого файла (порта): поверх него ставится
        io.BufferedReader с одним переиспользуемым буфером buffer_bytes — он
        дочитывает до размера, которого ждёт читатель IPC. Схема читается
        сразу, пачки — по мере итерации; чтение блокирующее и идёт в потоке."""
        buffered = io.BufferedReader(source, buffer_bytes)
        try:
            reader = await asyncio.to_thread(pyarrow.ipc.open_stream, buffered)
        except pyarrow.ArrowException as exc:
            raise OracleFormatError(
                f"reading an arrow ipc stream schema failed: {type(exc).__name__}: "
                f"{exc}"
            ) from exc

        return ArrowInbound(schema=reader.schema, batches=self._read_batches(reader))

    async def executemany_arrow(
        self, conn: AsyncConnection, text: str, batch: pyarrow.RecordBatch
    ) -> int:
        """Одна команда для пачки Arrow: драйвер берёт колонки пачки bind'ами
        `:1..:n` по порядку, значения в Python не разбираются. Транзакцию
        завершает вызывающий."""
        cursor = conn.cursor()
        try:
            await cursor.executemany(text, batch)
            affected = cursor.rowcount
        except oracledb.Error as exc:
            fields = ", ".join(f"{field.name} {field.type}" for field in batch.schema)
            raise OracleQueryError(
                f"executemany of an arrow batch on oracle failed for "
                f"{batch.num_rows} rows: {type(exc).__name__}: {exc}; "
                f"statement: {text[:200]!r}; batch columns: {fields}"
            ) from exc
        finally:
            cursor.close()

        return affected

    async def _read_batches(
        self, reader: pyarrow.ipc.RecordBatchStreamReader
    ) -> AsyncIterator[pyarrow.RecordBatch]:
        while True:
            try:
                batch = await asyncio.to_thread(self._next_batch, reader)
            except pyarrow.ArrowException as exc:
                raise OracleFormatError(
                    f"reading an arrow ipc batch failed: {type(exc).__name__}: {exc}"
                ) from exc

            if batch is None:
                return

            yield batch

    @staticmethod
    def _next_batch(
        reader: pyarrow.ipc.RecordBatchStreamReader,
    ) -> pyarrow.RecordBatch | None:
        try:
            return reader.read_next_batch()
        except StopIteration:
            return None

    async def _csv_batches(
        self, conn: AsyncConnection, text: str, schema: pyarrow.Schema
    ) -> AsyncIterator[memoryview]:
        options = pyarrow.csv.WriteOptions(include_header=False)
        async for table in self._tables(conn, text, schema):
            buffer = io.BytesIO()
            try:
                pyarrow.csv.write_csv(table, buffer, write_options=options)
            except pyarrow.ArrowException as exc:
                raise OracleQueryError(
                    f"writing an arrow batch as csv failed, every column must be "
                    f"text, number or date (binary needs rawtohex): "
                    f"{type(exc).__name__}: {exc}; query: {text[:200]!r}"
                ) from exc

            yield memoryview(buffer.getvalue())

    async def _tables(
        self, conn: AsyncConnection, text: str, schema: pyarrow.Schema
    ) -> AsyncIterator[pyarrow.Table]:
        """Пачки ответа драйвера таблицами Arrow по arraysize строк."""
        # у python-oracledb fetch_df_batches объявлен корутиной, на деле это
        # async-генератор: итератор берётся по протоколу, а не по аннотации
        batches = conn.fetch_df_batches(
            text, size=self._connection.arraysize, requested_schema=schema
        )
        open_iterator = getattr(batches, "__aiter__", None)
        if open_iterator is None:
            raise OracleQueryError(
                f"fetch_df_batches on oracle: expected an async iterator, got "
                f"{type(batches).__name__}; query: {text[:200]!r}"
            )

        try:
            async for batch in open_iterator():
                yield pyarrow.table(batch)
        except oracledb.Error as exc:
            raise OracleQueryError(
                f"reading arrow batches from oracle failed: {type(exc).__name__}: "
                f"{exc}; query: {text[:200]!r}"
            ) from exc
        except ValueError as exc:
            raise OracleQueryError(
                f"converting oracle values to arrow failed: {type(exc).__name__}: "
                f"{exc}; query: {text[:200]!r}"
            ) from exc

    @staticmethod
    async def _iterate(cursor: AsyncCursor, text: str) -> AsyncIterator[Sequence[Any]]:
        try:
            async for row in cursor:
                yield row
        except oracledb.Error as exc:
            raise OracleQueryError(
                f"reading rows from oracle failed: {type(exc).__name__}: {exc}; "
                f"query: {text[:200]!r}"
            ) from exc
