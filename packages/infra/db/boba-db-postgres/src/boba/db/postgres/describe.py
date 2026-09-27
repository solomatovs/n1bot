"""Описание колонок выборки postgres двумя вызовами libpq без выполнения
запроса: PQprepare (сообщение Parse с текстом select, сервер разбирает его и
резолвит имена по каталогу) и PQdescribePrepared (сообщение Describe S,
ответ RowDescription). Bind и Execute не отправляются: планирования и чтения
данных нет. Здесь же текст типа postgres по OID и typmod, как его печатает
format_type.

Ошибки:
PgDescribeError — сервер отклонил стейтмент на Parse или Describe, либо
    колонка без имени в RowDescription.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from enum import IntEnum, StrEnum
from typing import Any, ClassVar, Protocol

import psycopg
from psycopg import pq
from psycopg._typeinfo import TypeInfo
from psycopg.pq.abc import PGresult

from boba.db.postgres.errors import PgDescribeError

__all__ = [
    "PgDescribe",
    "PgDescribedColumn",
    "PgDigits",
    "PgFormat",
    "PgType",
    "PgTypeText",
    "PgTypedColumn",
    "PgTypmod",
]


class PgFormat(IntEnum):
    """Код формата значений колонки в протоколе (fformat в RowDescription)."""

    TEXT = 0
    BINARY = 1


class PgTypedColumn(Protocol):
    """Колонка postgres с тем, что нужно для вывода её типа: имя, OID типа и
    typmod. Реализации: PgDescribedColumn (RowDescription libpq) и
    PgCatalogColumn (pg_attribute приёмника)."""

    name: str
    oid: int
    typmod: int


@dataclass(frozen=True)
class PgDescribedColumn(PgTypedColumn):
    """Колонка выборки из RowDescription libpq, все поля сообщения: позиция
    (с нуля), имя, OID типа (ftype), typmod (fmod), OID таблицы (ftable, 0 у
    вычисляемой), номер атрибута в ней (ftablecol, с единицы, 0 у
    вычисляемой), размер типа в байтах (fsize, -1 у переменной длины), код
    формата значений (fformat). Nullable протокол не несёт."""

    position: int
    name: str
    oid: int
    typmod: int
    table_oid: int
    table_column: int
    size: int
    format: PgFormat


class PgType(StrEnum):
    """Имена типов postgres из реестра psycopg, у которых есть родной тип Arrow
    и которые читатель CSV разбирает сам, плюс time и timetz — они едут
    текстом, но в контракте несут единицу и пояс."""

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
    TIME = "time"
    TIMETZ = "timetz"


@dataclass(frozen=True)
class PgDigits:
    """Точность и масштаб numeric из typmod."""

    precision: int
    scale: int


class PgTypmod:
    """Декодер typmod — числа, в которое сервер упаковывает параметры типа
    своими typmodin-функциями: numeric — ((precision << 16) | scale) + 4,
    varchar и character — длина + 4, bit и bit varying — длина как есть,
    timestamp и time — точность как есть; -1 — параметров нет. Одно место
    распаковки для текста типа, сверки и схемы потока."""

    UNBOUNDED: ClassVar[int] = -1
    VARHDRSZ: ClassVar[int] = 4
    TIME_PRECISION: ClassVar[int] = 6
    NUMERIC: ClassVar[str] = "numeric"
    CHARS: ClassVar[frozenset[str]] = frozenset({"varchar", "bpchar"})
    BITS: ClassVar[frozenset[str]] = frozenset({"bit", "varbit"})
    PRECISE: ClassVar[frozenset[str]] = frozenset(
        {"timestamp", "timestamptz", "time", "timetz"}
    )

    def unbounded(self, typmod: int) -> bool:
        return typmod == self.UNBOUNDED

    def is_numeric(self, name: str) -> bool:
        return name == self.NUMERIC

    def is_sized(self, name: str) -> bool:
        return name in self.CHARS or name in self.BITS

    def is_precise(self, name: str) -> bool:
        return name in self.PRECISE

    def digits(self, typmod: int) -> PgDigits:
        """precision и scale numeric; у numeric без параметров — нули."""
        if self.unbounded(typmod):
            return PgDigits(precision=0, scale=0)

        packed = typmod - self.VARHDRSZ

        return PgDigits(precision=packed >> 16, scale=packed & 0xFFFF)

    def length(self, name: str, typmod: int) -> int:
        """Длина varchar и character в символах, bit и bit varying — в битах;
        0 — без предела."""
        if self.unbounded(typmod):
            return 0

        if name in self.BITS:
            return typmod

        if name in self.CHARS:
            return typmod - self.VARHDRSZ

        return 0

    def precision(self, typmod: int) -> int:
        """Точность timestamp и time; без параметра — 6, как у сервера."""
        if self.unbounded(typmod):
            return self.TIME_PRECISION

        return typmod


class PgTypeText:
    """Текст типа postgres по имени из реестра psycopg и typmod, как его
    печатает format_type: numeric(18,4), character varying(200),
    timestamp(3) without time zone; массив — тип элемента и []."""

    SQL_NAMES: ClassVar[Mapping[str, str]] = {
        "int2": "smallint",
        "int4": "integer",
        "int8": "bigint",
        "float4": "real",
        "float8": "double precision",
        "bool": "boolean",
        "varchar": "character varying",
        "bpchar": "character",
        "timestamp": "timestamp without time zone",
        "timestamptz": "timestamp with time zone",
        "time": "time without time zone",
        "timetz": "time with time zone",
        "varbit": "bit varying",
    }

    def __init__(self) -> None:
        self._typmod = PgTypmod()

    def render(self, info: TypeInfo, oid: int, typmod: int) -> str:
        if info.array_oid == oid:
            return f"{self.render(info, info.oid, typmod)}[]"

        name = self.SQL_NAMES.get(info.name, info.name)
        if self._typmod.unbounded(typmod):
            return name

        if self._typmod.is_numeric(info.name):
            digits = self._typmod.digits(typmod)

            return f"{name}({digits.precision},{digits.scale})"

        if self._typmod.is_sized(info.name):
            return f"{name}({self._typmod.length(info.name, typmod)})"

        if self._typmod.is_precise(info.name):
            head, _, tail = name.partition(" ")

            return f"{head}({self._typmod.precision(typmod)}) {tail}"

        return name

    def char_length(self, info: TypeInfo, oid: int, typmod: int) -> int:
        """Длина varchar/bpchar в символах, bit/varbit — в битах; 0 — без
        предела."""
        if info.array_oid == oid:
            return 0

        return self._typmod.length(info.name, typmod)


class PgDescribe:
    """Колонки выборки по RowDescription libpq на соединении: PQprepare +
    PQdescribePrepared, без Bind и Execute."""

    def __init__(self, conn: psycopg.AsyncConnection[Any]) -> None:
        self._conn = conn

    async def columns(self, query: str) -> AsyncIterator[PgDescribedColumn]:
        """Скрапим колонки указанного запроса, без выполнения запроса на сервере
        Делается это через вызовы libpq:
            - PQprepare - выполняет prepare запроса
            - PQdescribePrepared - возвращает описание колонок в prepare запросе
        """
        pgconn = self._conn.pgconn
        encoding = self._conn.info.encoding
        try:
            # pgconn.prepare(b"", text) это libpq-функция PQprepare
            # https://postgrespro.ru/docs/postgresql/current/libpq-exec#LIBPQ-PQPREPARE
            # Она отправляет одно сообщение протокола Parse
            # с пустым именем стейтмента b"" и текстом select
            # и ждёт ответ ParseComplete.
            # prepare синхронная поэтому помещена в asyncio.to_thread
            # Сервер на Parse делает две вещи:
            # - разбирает текст в дерево и анализирует его,
            #   то есть ищет таблицы, колонки, функции и операторы
            #   в каталоге и вычисляет тип каждого выражения результата.
            # Полученное дерево он сохраняет у себя в сессии
            # как безымянный подготовленный стейтмент.
            # Планировщик здесь не запускается, таблицы не читаются.
            # По смыслу это то же, что SQL-команда PREPARE, но без плана.
            prepared = await asyncio.to_thread(
                pgconn.prepare, b"", query.encode(encoding)
            )
            # проверяет статус ответа: если сервер вернул ошибку,
            # например «relation does not exist»,
            # она превращается в PgDescribeError с текстом запроса.
            self._ensure_ok(prepared, "preparing", query)
            # pgconn.describe_prepared(b"") это PQdescribePrepared.
            # https://postgrespro.ru/docs/postgresql/current/libpq-exec#LIBPQ-PQDESCRIBEPREPARED
            # Она отправляет сообщение Describe с типом S и пустым именем,
            # то есть "опиши сохранённый безымянный стейтмент".
            # Сервер отвечает ParameterDescription и RowDescription,
            # которую берёт из результата анализа на предыдущем шаге.
            # Ничего нового он не вычисляет, просто отдаёт список колонок:
            # имя, OID типа, typmod.
            described = await asyncio.to_thread(pgconn.describe_prepared, b"")
            self._ensure_ok(described, "describing", query)
        except psycopg.Error as exc:
            raise PgDescribeError(
                f"describing the statement on postgres failed: {type(exc).__name__}: "
                f"{exc}; query: {query[:200]!r}"
            ) from exc

        for position in range(described.nfields):
            name = described.fname(position)
            if name is None:
                raise PgDescribeError(
                    f"describing the statement on postgres: column {position} has "
                    f"no name; query: {query[:200]!r}"
                )

            # https://postgrespro.ru/docs/postgresql/current/libpq-exec#LIBPQ-PQFNAME
            # имя колонки
            column_name = name.decode(encoding)
            # OID типа данных колонки, число из pg_type.oid
            oid = described.ftype(position)
            # модификатор типа как одно число, -1 если не задан
            # из типа выражения:
            #   - numeric(18,4) упакованы precision и scale плюс 4,
            #   - varchar(200) длина плюс 4
            #   - timestamp(3) точность 3
            typmod = described.fmod(position)
            # OID таблицы в pg_class, если колонка взята из таблицы напрямую, иначе 0
            ftable = described.ftable(position)
            # номер атрибута в таблице {ftable} - pg_attribute.attnum, с единицы
            # 0, если колонка вычисляемая
            ftablecol = described.ftablecol(position)
            # код формата значений:
            #   0 текст
            #   1 бинарный
            fformat = PgFormat(described.fformat(position))
            # размер типа в байтах - pg_type.typlen
            #   -1 для типов переменной длины
            #   -2 для cstring
            # 4 для oid, 64 для name, 1 для "char"
            fsize = described.fsize(position)

            yield PgDescribedColumn(
                position=position,
                name=column_name,
                oid=oid,
                typmod=typmod,
                table_oid=ftable,
                table_column=ftablecol,
                size=fsize,
                format=fformat,
            )

    @staticmethod
    def _ensure_ok(result: PGresult, action: str, text: str) -> None:
        if result.status == pq.ExecStatus.COMMAND_OK:
            return

        message = result.error_message.decode(errors="replace").strip()
        raise PgDescribeError(
            f"{action} the statement on postgres failed: {message}; "
            f"query: {text[:200]!r}"
        )
