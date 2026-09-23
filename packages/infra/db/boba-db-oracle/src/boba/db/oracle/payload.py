"""Oracle для payload'ов и скраперов: thin-соединение python-oracledb по профилю,
строки запроса потоком с именованными bind'ами или CSV-байтами пачками Arrow,
запись пачками через executemany.

Ошибки:
OracleQueryError — сервер отклонил запрос или оборвал чтение (в том числе по
    call_timeout).
OracleError — до базы не достучаться: сеть, listener, отказ при входе.
"""

from __future__ import annotations

import io
from collections.abc import AsyncGenerator, AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, ClassVar

import oracledb
import pyarrow
import pyarrow.csv
from oracledb import (
    DB_TYPE_BINARY_DOUBLE,
    DB_TYPE_BINARY_FLOAT,
    DB_TYPE_BINARY_INTEGER,
    DB_TYPE_BLOB,
    DB_TYPE_DATE,
    DB_TYPE_LONG_RAW,
    DB_TYPE_NUMBER,
    DB_TYPE_RAW,
    DB_TYPE_TIMESTAMP,
    DB_TYPE_TIMESTAMP_LTZ,
    DB_TYPE_TIMESTAMP_TZ,
    AsyncConnection,
    AsyncCursor,
    DbType,
)

from boba.db.oracle.connection import OracleConfig
from boba.db.oracle.errors import OracleError, OracleQueryError
from boba.db.oracle.query import OraQueryBuilder, OraSql

__all__ = ["ByteStream", "OraColumnType", "PayloadOracle", "RowStream"]


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


class PayloadOracle:
    """Соединение по профилю и запросы на нём: строки потоком или CSV-байты пачками.

    Создаётся на профиль OracleConfig; из него берутся параметры соединения,
    call_timeout и arraysize курсоров. Пула нет: скрапер держит одно соединение на
    попытку, payload — на вызов. LOB-колонки читаются строками и байтами, а не
    объектами LOB, чтобы поток строк не зависел от открытого курсора; NUMBER
    приходит Decimal, а не float: битовые поля словаря шире 2^53.
    """

    # NUMBER без объявленной точности: столько знаков вмещает decimal128
    UNBOUNDED_NUMBER: ClassVar[pyarrow.DataType] = pyarrow.decimal128(38, 0)

    def __init__(self, connection: OracleConfig) -> None:
        self._connection = connection

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
        """Семейства типов колонок запроса по описанию курсора без выборки строк:
        запрос исполняется, но ни одна строка не читается."""
        cursor = await self._executed(conn, text, {})
        try:
            kinds: list[OraColumnType] = []
            for column in cursor.description or ():
                kinds.append(OraColumnType.of(column.type))
        finally:
            cursor.close()

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
        с пробелом. Типы колонок берутся у драйвера пустой пробой запроса; NUMBER
        без объявленной точности драйвер отдал бы double, поэтому такие колонки
        запрашиваются decimal128(38, 0): целые точны, дробное значение — ошибка
        DPY-4042, его запрос обязан привести сам (to_char или number(p, s)). RAW
        запрос отдаёт `rawtohex`: bytes в CSV не пишутся."""
        schema = await self._requested_schema(conn, text)

        yield ByteStream(
            names=self._schema_names(schema),
            blocks=self._csv_batches(conn, text, schema),
        )

    async def _requested_schema(
        self, conn: AsyncConnection, text: str
    ) -> pyarrow.Schema:
        """Схема ответа: типы драйвера по пустой выборке, NUMBER без точности —
        decimal128(38, 0)."""
        probe = (
            OraQueryBuilder(query=OraSql(text))
            .add("select * from ({query}) where rownum < 1")
            .build()
            .text
        )
        try:
            frame = await conn.fetch_df_all(probe)
        except oracledb.Error as exc:
            raise OracleQueryError(
                f"probing query schema on oracle failed: {type(exc).__name__}: "
                f"{exc}; query: {text[:200]!r}"
            ) from exc

        cursor = await self._executed(conn, probe, {})
        try:
            described = list(cursor.description or ())
        finally:
            cursor.close()

        fields: list[pyarrow.Field] = []
        for column, field in zip(described, pyarrow.table(frame).schema, strict=True):
            if column.type is DB_TYPE_NUMBER and column.precision == 0:
                fields.append(pyarrow.field(field.name, self.UNBOUNDED_NUMBER))
                continue

            fields.append(field)

        return pyarrow.schema(fields)

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

    async def _csv_batches(
        self, conn: AsyncConnection, text: str, schema: pyarrow.Schema
    ) -> AsyncIterator[memoryview]:
        options = pyarrow.csv.WriteOptions(include_header=False)
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
                table = pyarrow.table(batch)
                buffer = io.BytesIO()
                pyarrow.csv.write_csv(table, buffer, write_options=options)
                yield memoryview(buffer.getvalue())
        except oracledb.Error as exc:
            raise OracleQueryError(
                f"reading arrow batches from oracle failed: {type(exc).__name__}: "
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
