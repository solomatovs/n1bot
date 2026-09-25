"""Oracle для payload'ов и скраперов: thin-соединение python-oracledb по профилю,
строки запроса потоком с именованными bind'ами, CSV-байтами пачками Arrow
(блоками или прямо в файл) или потоком Arrow IPC в Arrow-порт; запись пачками
через executemany — строками или пачками Arrow; стейтменты before/after
насоса по одному на том же соединении.

Ошибки:
OracleQueryError — сервер отклонил запрос или оборвал чтение (в том числе по
    call_timeout).
OracleError — до базы не достучаться: сеть, listener, отказ при входе.
"""

from __future__ import annotations

import asyncio
import io
from collections.abc import AsyncGenerator, AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, ClassVar

import oracledb
import pyarrow
import pyarrow.csv
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
from boba.db.oracle.errors import OracleError, OracleQueryError
from boba.db.oracle.trace import OraScriptStep, OraSessionTrace
from boba.toolkit.arrow import ArrowIpc
from boba.toolkit.ports import ArrowOutbound

__all__ = [
    "ArrowTypes",
    "ByteStream",
    "PayloadOracle",
    "RowStream",
]


@dataclass(frozen=True)
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
        self._ipc = ArrowIpc()

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

    async def executemany(
        self,
        conn: AsyncConnection,
        text: str,
        rows: Sequence[Sequence[object]],
        trace: OraSessionTrace,
    ) -> int:
        """Одна команда для пачки строк: позиционные bind'ы `:1..:n` по порядку
        полей строки, драйвер шлёт пачку серверу одной поездкой. Значения идут
        как есть (текст CSV — строками, NULL — None), приводит их сам стейтмент.
        Итог курсора уходит в trace; возвращает число затронутых строк,
        транзакцию завершает вызывающий."""
        cursor = conn.cursor()
        try:
            await cursor.executemany(text, list(rows))
            trace.took(cursor)
            affected = cursor.rowcount
        except oracledb.Error as exc:
            raise OracleQueryError(
                f"executemany on oracle failed for {len(rows)} rows: "
                f"{type(exc).__name__}: {exc}; statement: {text[:200]!r}"
            ) from exc
        finally:
            cursor.close()

        return affected

    async def script(
        self, conn: AsyncConnection, statements: Sequence[str], trace: OraSessionTrace
    ) -> tuple[OraScriptStep, ...]:
        """Стейтменты before/after насоса по одному, по порядку, на том же
        соединении: DML остаётся в транзакции насоса до commit вызывающего,
        DDL Oracle фиксирует сам. Строки выборок не собираются, шаг даёт
        число затронутых строк; предупреждения курсоров уходят в trace."""
        steps: list[OraScriptStep] = []
        for statement in statements:
            cursor = await self._executed(conn, statement, {})
            try:
                trace.warned(cursor)
                affected = self._affected(cursor)
            finally:
                cursor.close()

            steps.append(OraScriptStep(statement, affected))

        return tuple(steps)

    @staticmethod
    def _affected(cursor: AsyncCursor) -> int | None:
        if cursor.description is not None:
            return None

        if cursor.rowcount < 0:
            return None

        return cursor.rowcount

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
        self,
        conn: AsyncConnection,
        text: str,
        sink: ArrowOutbound,
        trace: OraSessionTrace,
    ) -> pyarrow.Schema:
        """Ответ запроса потоком Arrow IPC в выходной порт: схема, затем пачки
        драйвера по arraysize строк как есть, без перевода в текст. Схема та
        же, что у csv (ArrowTypes); типы, которые драйвер в Arrow не отдаёт,
        отвергаются до выполнения. Прочитанные строки считает trace."""
        schema = await self._requested_schema(conn, text)
        writer = await self._ipc.open_out(sink, schema)
        async for table in self._tables(conn, text, schema):
            trace.took_rows(table.num_rows)
            try:
                await writer.write(table)
            except pyarrow.ArrowException as exc:
                raise OracleQueryError(
                    f"writing an arrow batch as ipc failed, the batch schema "
                    f"{table.schema} differs from {schema}: {type(exc).__name__}: "
                    f"{exc}; query: {text[:200]!r}"
                ) from exc

        await writer.close()

        return schema

    async def csv_into(
        self,
        conn: AsyncConnection,
        text: str,
        sink: io.RawIOBase,
        trace: OraSessionTrace,
    ) -> tuple[str, ...]:
        """Ответ запроса CSV-байтами без заголовка прямо в двоичный файл (сырой
        порт): pyarrow пишет каждую пачку в sink сам, без промежуточного
        буфера. Правила формата — как у csv. Запись блокирующая и идёт в
        потоке. Прочитанные строки считает trace."""
        schema = await self._requested_schema(conn, text)
        options = pyarrow.csv.WriteOptions(include_header=False)
        async for table in self._tables(conn, text, schema):
            trace.took_rows(table.num_rows)
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

    async def executemany_arrow(
        self,
        conn: AsyncConnection,
        text: str,
        batch: pyarrow.RecordBatch,
        trace: OraSessionTrace,
    ) -> int:
        """Одна команда для пачки Arrow: драйвер берёт колонки пачки bind'ами
        `:1..:n` по порядку, значения в Python не разбираются. Итог курсора
        уходит в trace; возвращает число затронутых строк, транзакцию
        завершает вызывающий."""
        cursor = conn.cursor()
        try:
            await cursor.executemany(text, batch)
            trace.took(cursor)
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
