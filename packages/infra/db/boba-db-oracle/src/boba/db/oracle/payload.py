"""Oracle для payload'ов и скраперов: thin-соединение python-oracledb по профилю,
строки запроса потоком с именованными bind'ами или потоком Arrow IPC в
Arrow-порт (пачки драйвера как есть, без объектов Python); запись пачками
Arrow через executemany; стейтменты before/after насоса по одному на том же
соединении.

Ошибки:
OracleQueryError — сервер отклонил запрос или оборвал чтение (в том числе по
    call_timeout).
OracleError — до базы не достучаться: сеть, listener, отказ при входе.
"""

from __future__ import annotations

import io
from collections.abc import AsyncGenerator, AsyncIterator, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, ClassVar

import oracledb
import pyarrow
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
from boba.db.oracle.errors import (
    OracleError,
    OracleMissingObjectError,
    OracleQueryError,
)
from boba.db.oracle.trace import OraScriptStep, OraSessionTrace
from boba.toolkit.arrow import ArrowColumns, ArrowIpc, SourceFields
from boba.toolkit.sync import ColumnSpec, ColumnType, TimeUnit, TypeFamily

__all__ = [
    "ArrowTypes",
    "OraColumnKinds",
    "OraTypeText",
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


class OraTypeText:
    """Текст типа Oracle по описанию колонки драйвера, как в DDL:
    NUMBER(18,4), VARCHAR2(200), VARCHAR2(50 CHAR), TIMESTAMP(6),
    INTERVAL DAY(2) TO SECOND(6). Длина строк — в символах (display_size);
    когда байтов под неё отведено больше, чем символов, семантика CHAR.
    Длина RAW — в байтах. LOB драйвер описывает как LONG, потому что
    читаются они строками и байтами (fetch_lobs выключен): текст типа —
    CLOB, NCLOB, BLOB, ими же создаётся колонка приёмника."""

    NAMES: ClassVar[Mapping[str, str]] = {
        "DB_TYPE_VARCHAR": "VARCHAR2",
        "DB_TYPE_NVARCHAR": "NVARCHAR2",
        "DB_TYPE_CHAR": "CHAR",
        "DB_TYPE_NCHAR": "NCHAR",
        "DB_TYPE_LONG": "CLOB",
        "DB_TYPE_LONG_NVARCHAR": "NCLOB",
        "DB_TYPE_CLOB": "CLOB",
        "DB_TYPE_NCLOB": "NCLOB",
        "DB_TYPE_RAW": "RAW",
        "DB_TYPE_LONG_RAW": "BLOB",
        "DB_TYPE_BLOB": "BLOB",
        "DB_TYPE_NUMBER": "NUMBER",
        "DB_TYPE_BINARY_INTEGER": "BINARY_INTEGER",
        "DB_TYPE_BINARY_FLOAT": "BINARY_FLOAT",
        "DB_TYPE_BINARY_DOUBLE": "BINARY_DOUBLE",
        "DB_TYPE_DATE": "DATE",
        "DB_TYPE_TIMESTAMP": "TIMESTAMP",
        "DB_TYPE_TIMESTAMP_TZ": "TIMESTAMP WITH TIME ZONE",
        "DB_TYPE_TIMESTAMP_LTZ": "TIMESTAMP WITH LOCAL TIME ZONE",
        "DB_TYPE_BOOLEAN": "BOOLEAN",
        "DB_TYPE_JSON": "JSON",
        "DB_TYPE_XMLTYPE": "XMLTYPE",
        "DB_TYPE_ROWID": "ROWID",
        "DB_TYPE_UROWID": "UROWID",
        "DB_TYPE_VECTOR": "VECTOR",
        "DB_TYPE_INTERVAL_YM": "INTERVAL YEAR TO MONTH",
        "DB_TYPE_INTERVAL_DS": "INTERVAL DAY TO SECOND",
    }
    TEXTS: ClassVar[frozenset[str]] = frozenset(
        {"DB_TYPE_VARCHAR", "DB_TYPE_NVARCHAR", "DB_TYPE_CHAR", "DB_TYPE_NCHAR"}
    )
    BYTE_SEMANTIC: ClassVar[frozenset[str]] = frozenset(
        {"DB_TYPE_VARCHAR", "DB_TYPE_CHAR"}
    )
    RAW: ClassVar[str] = "DB_TYPE_RAW"
    TIMESTAMPS: ClassVar[frozenset[str]] = frozenset(
        {"DB_TYPE_TIMESTAMP", "DB_TYPE_TIMESTAMP_TZ", "DB_TYPE_TIMESTAMP_LTZ"}
    )
    INTERVAL_DS: ClassVar[str] = "DB_TYPE_INTERVAL_DS"
    INTERVAL_YM: ClassVar[str] = "DB_TYPE_INTERVAL_YM"
    CHAR_SEMANTIC: ClassVar[str] = "CHAR"
    FLOAT_SCALE: ClassVar[int] = -127

    def render(self, column: FetchInfo) -> str:
        code = column.type.name
        name = self.NAMES.get(code, code)
        renderers: Mapping[str, Callable[[str, FetchInfo], str]] = {
            "DB_TYPE_NUMBER": self._number,
            self.RAW: self._raw,
            self.INTERVAL_DS: self._interval_ds,
            self.INTERVAL_YM: self._interval_ym,
        }
        render = renderers.get(code)
        if render is not None:
            return render(name, column)

        if code in self.TEXTS:
            return self._text(name, column)

        if code in self.TIMESTAMPS:
            return self._timestamp(name, column)

        return name

    def char_length(self, column: FetchInfo) -> int:
        """Длина в символах у текстов, в байтах у RAW, 0 у остальных."""
        code = column.type.name
        if code == self.RAW:
            return self._size(column.internal_size)

        if code not in self.TEXTS:
            return 0

        return self._size(column.display_size)

    @staticmethod
    def _raw(name: str, column: FetchInfo) -> str:
        return f"{name}({column.internal_size})"

    @staticmethod
    def _interval_ds(name: str, column: FetchInfo) -> str:
        return f"INTERVAL DAY({column.precision}) TO SECOND({column.scale})"

    @staticmethod
    def _interval_ym(name: str, column: FetchInfo) -> str:
        return f"INTERVAL YEAR({column.precision}) TO MONTH"

    def _text(self, name: str, column: FetchInfo) -> str:
        chars = self._size(column.display_size)
        semantic = ""
        if column.type.name in self.BYTE_SEMANTIC:
            if self._size(column.internal_size) > chars:
                semantic = f" {self.CHAR_SEMANTIC}"

        return f"{name}({chars}{semantic})"

    @staticmethod
    def _timestamp(name: str, column: FetchInfo) -> str:
        head, _, tail = name.partition(" ")
        if column.scale is None:
            return name

        return f"{head}({column.scale}) {tail}".rstrip()

    @staticmethod
    def _size(value: int | None) -> int:
        if value is None:
            return 0

        return int(value)

    def _number(self, name: str, column: FetchInfo) -> str:
        precision = column.precision
        scale = column.scale
        if precision is None or scale is None:
            return name

        if precision == 0:
            return name

        if scale == self.FLOAT_SCALE:
            return f"FLOAT({precision})"

        return f"{name}({precision},{scale})"


class ArrowTypes:
    """Тип Arrow для колонки по описанию курсора после parse — та же раскладка,
    что драйвер выбирает сам при fetch_df, кроме NUMBER: без точности он отдал
    бы double, поэтому NUMBER(p, s) запрашивается decimal128(p, s), целый
    NUMBER(p, 0) до 18 знаков — int64, без точности — decimal128(38, 0),
    NUMBER(p, -s) — decimal128(p + s, 0), FLOAT(p) — double. Типы, которые
    драйвер в Arrow не отдаёт (INTERVAL, XMLTYPE, JSON, ROWID, VECTOR,
    объекты) или отдаёт с потерей (TIMESTAMP WITH TIME ZONE без смещения),
    отвергаются до выполнения запроса с подсказкой, чем их привести в самом
    select."""

    # NUMBER без объявленной точности: столько знаков вмещает decimal128
    UNBOUNDED_NUMBER: ClassVar[pyarrow.DataType] = pyarrow.decimal128(38, 0)
    # масштаб, которым драйвер помечает FLOAT(p) и NUMBER без точности
    FLOAT_SCALE: ClassVar[int] = -127
    # целый NUMBER(p, 0) до стольких знаков помещается в int64
    INT64_DIGITS: ClassVar[int] = 18
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
    TIMESTAMPS: ClassVar[frozenset[DbType]] = frozenset({DB_TYPE_TIMESTAMP})
    FIXED: ClassVar[Mapping[DbType, pyarrow.DataType]] = {
        DB_TYPE_BINARY_INTEGER: pyarrow.int64(),
        DB_TYPE_BINARY_DOUBLE: pyarrow.float64(),
        DB_TYPE_BINARY_FLOAT: pyarrow.float32(),
        DB_TYPE_DATE: pyarrow.timestamp("s"),
        DB_TYPE_BOOLEAN: pyarrow.bool_(),
    }
    HINTS: ClassVar[Mapping[str, str]] = {
        "DB_TYPE_TIMESTAMP_TZ": "the offset would be lost: sys_extract_utc(col) "
        "for the UTC wall time (declare a zoned type on the target), or "
        "to_char(col, 'yyyy-mm-dd hh24:mi:ss.ff6tzh:tzm') for text with the offset",
        "DB_TYPE_TIMESTAMP_LTZ": "the zone would be lost: sys_extract_utc(col) "
        "for the UTC wall time (declare a zoned type on the target), or "
        "to_char(col, 'yyyy-mm-dd hh24:mi:ss.ff6tzh:tzm') for text with the offset",
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

    ENGINE: ClassVar[str] = "oracle"

    def __init__(self) -> None:
        self._text = OraTypeText()
        self._fields = SourceFields(self.ENGINE)

    def schema(self, described: Sequence[FetchInfo]) -> pyarrow.Schema:
        """Схема потока с metadata контракта приёмника: текст типа Oracle,
        nullable из null_ok драйвера, длина строк и RAW из internal_size."""
        fields: list[pyarrow.Field] = []
        for column in described:
            fields.append(
                self._fields.field(
                    column.name,
                    self.of(column),
                    bool(column.null_ok),
                    self._text.render(column),
                    self._text.char_length(column),
                )
            )

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

        if scale == 0 and precision <= self.INT64_DIGITS:
            return pyarrow.int64()

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


class OraColumnKinds:
    """Колонка по описанию драйвера (FetchInfo) в нейтральном виде для
    сверки — для любого типа, в том числе того, что Arrow не везёт: так
    описываются колонки таблицы-приёмника и типы из rules.column_types.
    Семейство и параметры совпадают с тем, что даёт контракт источника
    через ArrowTypes: целый NUMBER до 18 знаков — integer 64, NUMBER(p, s)
    — decimal, FLOAT(p) — float 64, DATE — момент в секундах, TIMESTAMP —
    секунды, микросекунды или наносекунды по долям; NUMBER без точности —
    decimal без точности (в него ложится любое число). Длина строк — в
    символах, текст типа — как в DDL (OraTypeText)."""

    FLOAT_SCALE: ClassVar[int] = -127
    INT64_DIGITS: ClassVar[int] = 18
    MICROSECONDS: ClassVar[int] = 6
    TEXT: ClassVar[frozenset[DbType]] = ArrowTypes.TEXT
    BINARY: ClassVar[frozenset[DbType]] = ArrowTypes.BINARY
    TIMESTAMPS: ClassVar[frozenset[DbType]] = frozenset(
        {DB_TYPE_TIMESTAMP, DB_TYPE_TIMESTAMP_TZ, DB_TYPE_TIMESTAMP_LTZ}
    )
    ZONED: ClassVar[frozenset[DbType]] = frozenset(
        {DB_TYPE_TIMESTAMP_TZ, DB_TYPE_TIMESTAMP_LTZ}
    )
    BITS: ClassVar[Mapping[DbType, int]] = {
        DB_TYPE_BINARY_FLOAT: 32,
        DB_TYPE_BINARY_DOUBLE: 64,
        DB_TYPE_BINARY_INTEGER: 64,
    }
    FAMILIES: ClassVar[Mapping[str, TypeFamily]] = {
        "DB_TYPE_BINARY_FLOAT": TypeFamily.FLOAT,
        "DB_TYPE_BINARY_DOUBLE": TypeFamily.FLOAT,
        "DB_TYPE_BINARY_INTEGER": TypeFamily.INTEGER,
        "DB_TYPE_BOOLEAN": TypeFamily.BOOLEAN,
        "DB_TYPE_JSON": TypeFamily.JSON,
        "DB_TYPE_XMLTYPE": TypeFamily.XML,
        "DB_TYPE_INTERVAL_DS": TypeFamily.INTERVAL,
        "DB_TYPE_INTERVAL_YM": TypeFamily.INTERVAL,
    }

    def __init__(self) -> None:
        self._text = OraTypeText()

    def specs(self, described: Sequence[FetchInfo]) -> tuple[ColumnSpec, ...]:
        """Колонки стейтмента: имя строчными, как их сверяет приёмник."""
        specs: list[ColumnSpec] = []
        for position, column in enumerate(described):
            specs.append(
                ColumnSpec(
                    name=column.name.lower(),
                    kind=self.kind(column),
                    nullable=bool(column.null_ok),
                    position=position,
                    source_type=self._text.render(column),
                    char_length=self._text.char_length(column),
                )
            )

        return tuple(specs)

    def kind(self, column: FetchInfo) -> ColumnType:
        text = self._text.render(column)
        kind = column.type
        if kind is DB_TYPE_NUMBER:
            return self._number(column, text)

        if kind is DB_TYPE_DATE:
            return ColumnType(TypeFamily.TIMESTAMP, text, unit=TimeUnit.SECOND)

        if kind in self.TIMESTAMPS:
            return ColumnType(
                TypeFamily.TIMESTAMP,
                text,
                unit=self._unit(column.scale),
                zoned=kind in self.ZONED,
            )

        if kind in self.TEXT:
            return ColumnType(TypeFamily.STRING, text)

        if kind in self.BINARY:
            return ColumnType(TypeFamily.BINARY, text)

        family = self.FAMILIES.get(kind.name, TypeFamily.OTHER)

        return ColumnType(family, text, bits=self.BITS.get(kind, 0))

    def _number(self, column: FetchInfo, text: str) -> ColumnType:
        precision = column.precision
        scale = column.scale
        if not precision or scale is None:
            return ColumnType(TypeFamily.DECIMAL, text)

        if scale == self.FLOAT_SCALE:
            return ColumnType(TypeFamily.FLOAT, text, bits=64)

        if scale < 0:
            return ColumnType(TypeFamily.DECIMAL, text, precision=precision - scale)

        if scale == 0 and precision <= self.INT64_DIGITS:
            return ColumnType(TypeFamily.INTEGER, text, bits=64)

        return ColumnType(TypeFamily.DECIMAL, text, precision=precision, scale=scale)

    def _unit(self, fraction: int | None) -> TimeUnit:
        if fraction is None:
            return TimeUnit.MICROSECOND

        if fraction == 0:
            return TimeUnit.SECOND

        if fraction <= self.MICROSECONDS:
            return TimeUnit.MICROSECOND

        return TimeUnit.NANOSECOND


class PayloadOracle:
    """Соединение по профилю и запросы на нём: строки потоком или пачки Arrow.

    Создаётся на профиль OracleConfig; из него берутся параметры соединения,
    call_timeout и arraysize курсоров. Пула нет: скрапер держит одно соединение на
    попытку, payload — на вызов. LOB-колонки читаются строками и байтами, а не
    объектами LOB, чтобы поток строк не зависел от открытого курсора; NUMBER
    приходит Decimal, а не float: битовые поля словаря шире 2^53.
    """

    MISSING_OBJECT: ClassVar[int] = 942

    def __init__(self, connection: OracleConfig) -> None:
        self._connection = connection
        self._types = ArrowTypes()
        self._ipc = ArrowIpc()
        self._columns = ArrowColumns()

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
                blocks=self._iterate(cursor),
                affected=cursor.rowcount,
            )
        finally:
            cursor.close()

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

    async def _requested_schema(
        self, conn: AsyncConnection, text: str
    ) -> pyarrow.Schema:
        """Схема ответа по описанию колонок после parse — запрос не выполняется."""
        return self._types.schema(await self.describe(conn, text))

    async def describe_specs(
        self, conn: AsyncConnection, text: str
    ) -> tuple[ColumnSpec, ...]:
        """Контракт колонок ответа по описанию после parse: типы, null_ok и
        тексты типов Oracle из metadata схемы Arrow."""
        return self._columns.specs(await self._requested_schema(conn, text))

    async def describe(self, conn: AsyncConnection, text: str) -> tuple[FetchInfo, ...]:
        """Колонки стейтмента: parse на сервере без выполнения. На время parse
        кэш стейтментов соединения выключен: разобранный стейтмент в кэше
        ломает последующую Arrow-выборку того же текста (DPY-5002 в
        python-oracledb 26). Объекта нет — OracleMissingObjectError."""
        cache_size = conn.stmtcachesize
        conn.stmtcachesize = 0
        cursor = conn.cursor()
        try:
            await cursor.parse(text)
            described = tuple(cursor.description or ())
        except oracledb.Error as exc:
            message = (
                f"parsing the statement on oracle failed: {type(exc).__name__}: "
                f"{exc}; query: {text[:200]!r}"
            )
            if self._code(exc) == self.MISSING_OBJECT:
                raise OracleMissingObjectError(message) from exc

            raise OracleQueryError(message) from exc
        finally:
            cursor.close()
            conn.stmtcachesize = cache_size

        return described

    @staticmethod
    def _code(exc: oracledb.Error) -> int:
        """Код ORA из ошибки драйвера; 0, если драйвер его не сообщил."""
        details = exc.args[0]
        code = getattr(details, "code", 0)
        if not isinstance(code, int):
            return 0

        return code

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
        sink: io.RawIOBase,
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
    async def _iterate(cursor: AsyncCursor) -> AsyncIterator[Sequence[Any]]:
        """Строки курсора; текст запроса для ошибки курсор хранит сам."""
        try:
            async for row in cursor:
                yield row
        except oracledb.Error as exc:
            statement = str(cursor.statement or "")
            raise OracleQueryError(
                f"reading rows from oracle failed: {type(exc).__name__}: {exc}; "
                f"query: {statement[:200]!r}"
            ) from exc
