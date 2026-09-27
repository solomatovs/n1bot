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
from collections.abc import (
    AsyncIterator,
    Callable,
    Mapping,
    Sequence,
)
from dataclasses import dataclass
from enum import IntEnum, StrEnum
from typing import Any, ClassVar, Protocol

import psycopg
import pyarrow
import pyarrow.compute
import pyarrow.csv
from psycopg import pq, sql
from psycopg._typeinfo import TypeInfo, TypesRegistry
from psycopg.pq.abc import PGresult

from boba.db.postgres.errors import PgArrowError
from boba.db.postgres.query import PgQuery, PgQueryBuilder
from boba.db.postgres.trace import PgCommandReport, PgSessionTrace
from boba.toolkit.arrow import (
    ArrowColumns,
    ArrowIpc,
    ArrowReader,
    BytePipe,
    SourceFields,
)
from boba.toolkit.sync import ColumnSpec, ColumnType, TimeUnit, TypeFamily

__all__ = [
    "Compute",
    "HexFloats",
    "PgArrowIn",
    "PgArrowOut",
    "PgArrowTypes",
    "PgDescribedColumn",
    "PgFormat",
    "PgTypeText",
    "PgTypedColumn",
]


class PgFormat(IntEnum):
    """Код формата значений колонки в протоколе (fformat в RowDescription)."""

    TEXT = 0
    BINARY = 1


class PgTypedColumn(Protocol):
    """Колонка postgres с тем, что нужно PgArrowTypes для вывода типа: имя,
    OID типа и typmod. Реализации: PgDescribedColumn (RowDescription libpq)
    и PgCatalogColumn (pg_attribute приёмника)."""

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


class CsvText(StrEnum):
    """Как postgres пишет CSV: истина и ложь у boolean, NULL — пустое поле."""

    TRUE = "t"
    FALSE = "f"
    NULL = ""


class PgTypeText:
    """Текст типа postgres по имени из реестра psycopg и typmod, как его
    печатает format_type: numeric(18,4), character varying(200),
    timestamp(3) without time zone; массив — тип элемента и []. Длина
    строки у character varying и character — typmod без заголовка."""

    VARHDRSZ: ClassVar[int] = 4
    UNBOUNDED: ClassVar[int] = -1
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
    SIZED: ClassVar[frozenset[str]] = frozenset({"varchar", "bpchar", "bit", "varbit"})
    PRECISE: ClassVar[frozenset[str]] = frozenset(
        {"timestamp", "timestamptz", "time", "timetz"}
    )

    def render(self, info: TypeInfo, oid: int, typmod: int) -> str:
        if info.array_oid == oid:
            return f"{self.render(info, info.oid, typmod)}[]"

        name = self.SQL_NAMES.get(info.name, info.name)
        if info.name == PgType.NUMERIC:
            return self._numeric(name, typmod)

        if info.name in self.SIZED:
            return self._sized(name, typmod)

        if info.name in self.PRECISE:
            return self._precise(name, typmod)

        return name

    def char_length(self, info: TypeInfo, oid: int, typmod: int) -> int:
        """Длина varchar/bpchar в символах, bit/varbit — в битах; 0 — без
        предела."""
        if info.array_oid == oid:
            return 0

        if typmod == self.UNBOUNDED:
            return 0

        if info.name in ("bit", "varbit"):
            return typmod

        if info.name not in ("varchar", "bpchar"):
            return 0

        return typmod - self.VARHDRSZ

    def _numeric(self, name: str, typmod: int) -> str:
        if typmod == self.UNBOUNDED:
            return name

        packed = typmod - self.VARHDRSZ
        precision = packed >> 16
        scale = packed & 0xFFFF

        return f"{name}({precision},{scale})"

    def _sized(self, name: str, typmod: int) -> str:
        if typmod == self.UNBOUNDED:
            return name

        if name.startswith("bit"):
            return f"{name}({typmod})"

        return f"{name}({typmod - self.VARHDRSZ})"

    def _precise(self, name: str, typmod: int) -> str:
        if typmod == self.UNBOUNDED:
            return name

        head, _, tail = name.partition(" ")

        return f"{head}({typmod}) {tail}"


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
    MILLISECONDS: ClassVar[int] = 3
    FLOAT32_BITS: ClassVar[int] = 32
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

    FAMILIES: ClassVar[Mapping[str, TypeFamily]] = {
        "text": TypeFamily.STRING,
        "varchar": TypeFamily.STRING,
        "bpchar": TypeFamily.STRING,
        "name": TypeFamily.STRING,
        '"char"': TypeFamily.STRING,
        "bytea": TypeFamily.BINARY,
        "uuid": TypeFamily.UUID,
        "json": TypeFamily.JSON,
        "jsonb": TypeFamily.JSON,
        "jsonpath": TypeFamily.JSON,
        "interval": TypeFamily.INTERVAL,
        "inet": TypeFamily.NETWORK,
        "cidr": TypeFamily.NETWORK,
        "macaddr": TypeFamily.NETWORK,
        "macaddr8": TypeFamily.NETWORK,
        "bit": TypeFamily.BIT,
        "varbit": TypeFamily.BIT,
        "money": TypeFamily.MONEY,
        "xml": TypeFamily.XML,
        "point": TypeFamily.GEOMETRY,
        "line": TypeFamily.GEOMETRY,
        "lseg": TypeFamily.GEOMETRY,
        "box": TypeFamily.GEOMETRY,
        "path": TypeFamily.GEOMETRY,
        "polygon": TypeFamily.GEOMETRY,
        "circle": TypeFamily.GEOMETRY,
        "int4range": TypeFamily.RANGE,
        "int8range": TypeFamily.RANGE,
        "numrange": TypeFamily.RANGE,
        "tsrange": TypeFamily.RANGE,
        "tstzrange": TypeFamily.RANGE,
        "daterange": TypeFamily.RANGE,
        "int4multirange": TypeFamily.RANGE,
        "int8multirange": TypeFamily.RANGE,
        "nummultirange": TypeFamily.RANGE,
        "tsmultirange": TypeFamily.RANGE,
        "tstzmultirange": TypeFamily.RANGE,
        "datemultirange": TypeFamily.RANGE,
        "tsvector": TypeFamily.TEXTSEARCH,
        "tsquery": TypeFamily.TEXTSEARCH,
        "gtsvector": TypeFamily.TEXTSEARCH,
        "cid": TypeFamily.SYSTEM,
        "tid": TypeFamily.SYSTEM,
        "xid": TypeFamily.SYSTEM,
        "xid8": TypeFamily.SYSTEM,
        "oidvector": TypeFamily.SYSTEM,
        "int2vector": TypeFamily.SYSTEM,
        "aclitem": TypeFamily.SYSTEM,
        "refcursor": TypeFamily.SYSTEM,
        "pg_lsn": TypeFamily.SYSTEM,
        "txid_snapshot": TypeFamily.SYSTEM,
        "regclass": TypeFamily.SYSTEM,
        "regcollation": TypeFamily.SYSTEM,
        "regconfig": TypeFamily.SYSTEM,
        "regdictionary": TypeFamily.SYSTEM,
        "regnamespace": TypeFamily.SYSTEM,
        "regoper": TypeFamily.SYSTEM,
        "regoperator": TypeFamily.SYSTEM,
        "regproc": TypeFamily.SYSTEM,
        "regprocedure": TypeFamily.SYSTEM,
        "regrole": TypeFamily.SYSTEM,
        "regtype": TypeFamily.SYSTEM,
        "record": TypeFamily.OTHER,
    }
    """Семейство контракта для каждого имени реестра psycopg, кроме тех,
    что везёт Arrow (ARROW, numeric, time, timetz); значения всех этих
    типов едут текстом сервера."""

    PLAIN: ClassVar[Mapping[TypeFamily, pyarrow.DataType]] = {
        TypeFamily.BOOLEAN: pyarrow.bool_(),
        TypeFamily.DATE: pyarrow.date32(),
    }
    SIGNED: ClassVar[Mapping[int, pyarrow.DataType]] = {
        8: pyarrow.int16(),
        16: pyarrow.int16(),
        32: pyarrow.int32(),
        64: pyarrow.int64(),
    }
    UNSIGNED: ClassVar[Mapping[int, pyarrow.DataType]] = {
        8: pyarrow.uint16(),
        16: pyarrow.uint16(),
        32: pyarrow.uint32(),
        64: pyarrow.uint64(),
    }

    ENGINE: ClassVar[str] = "postgres"

    def __init__(self, registry: TypesRegistry) -> None:
        self._registry = registry
        self._text = PgTypeText()
        self._fields = SourceFields(self.ENGINE)
        self._columns = ArrowColumns()

    async def schema(self, columns: AsyncIterator[PgDescribedColumn]) -> pyarrow.Schema:
        """pyarrow схема из postgres колонок"""
        fields: list[pyarrow.Field] = []
        async for column in columns:
            fields.append(
                self._fields.field(
                    name=column.name,
                    kind=self.of(column),
                    nullable=True,
                    source_type=self.source_type(column),
                    char_length=self.char_length(column),
                )
            )

        return pyarrow.schema(fields)

    def schema_of(self, specs: Sequence[ColumnSpec]) -> pyarrow.Schema:
        """Схема потока Arrow по контракту (описание драйвера, слитое с
        декларациями LLM): тип Arrow из семейства и параметров, metadata —
        текст типа, длина строки и nullable контракта."""
        fields: list[pyarrow.Field] = []
        for spec in specs:
            fields.append(
                self._fields.field(
                    spec.name,
                    self.arrow_of(spec),
                    spec.nullable,
                    spec.source_type,
                    spec.char_length,
                )
            )

        return pyarrow.schema(fields)

    def arrow_of(self, spec: ColumnSpec) -> pyarrow.DataType:
        """Тип Arrow по нейтральному типу контракта: числа, boolean, date,
        timestamp — родными, decimal — decimal128 по precision/scale (без
        точности или шире 38 знаков — отказ), всё остальное — large_string."""
        kind = spec.kind
        renderers: Mapping[TypeFamily, Callable[[ColumnSpec], pyarrow.DataType]] = {
            TypeFamily.DECIMAL: self._decimal_of,
            TypeFamily.TIMESTAMP: self._timestamp_of,
            TypeFamily.INTEGER: self._integer_of,
            TypeFamily.FLOAT: self._float_of,
        }
        renderer = renderers.get(kind.family)
        if renderer is not None:
            return renderer(spec)

        plain = self.PLAIN.get(kind.family)
        if plain is not None:
            return plain

        return pyarrow.large_string()

    def _decimal_of(self, spec: ColumnSpec) -> pyarrow.DataType:
        return self._decimal_arrow(spec.name, spec.kind.precision, spec.kind.scale)

    def _timestamp_of(self, spec: ColumnSpec) -> pyarrow.DataType:
        if spec.kind.zoned:
            return pyarrow.timestamp(spec.kind.unit.value, "UTC")

        return pyarrow.timestamp(spec.kind.unit.value)

    def _float_of(self, spec: ColumnSpec) -> pyarrow.DataType:
        if spec.kind.bits == self.FLOAT32_BITS:
            return pyarrow.float32()

        return pyarrow.float64()

    def _integer_of(self, spec: ColumnSpec) -> pyarrow.DataType:
        widths = self.SIGNED
        if spec.kind.unsigned:
            widths = self.UNSIGNED

        arrow = widths.get(spec.kind.bits)
        if arrow is None:
            return pyarrow.int64()

        return arrow

    def _decimal_arrow(self, name: str, precision: int, scale: int) -> pyarrow.DataType:
        if precision == 0:
            raise PgArrowError(
                f"column {name} is numeric without precision: the scale of its "
                f"values is unknown, cast it in the select: {name}::numeric(p, s) "
                f"or {name}::text, or declare precision and scale in columns"
            )

        if precision <= self.DECIMAL128_DIGITS:
            return pyarrow.decimal128(precision, scale)

        raise PgArrowError(
            f"column {name} is numeric({precision}, {scale}): the csv reader of "
            f"arrow holds decimals up to {self.DECIMAL128_DIGITS} digits, cast it "
            f"in the select: {name}::text, or declare family string in columns"
        )

    def kind_of(self, column: PgTypedColumn) -> ColumnType:
        """Формирует нейтральный тип данных из нативных Postgres типов

        То, что невозможно преобразовать (слишком postgres специфичны)
        проставляются как other с именем которое отдает сам postgres
        Там где postgres не возвращает даже имени, проставляется OID номер
        """
        info: TypeInfo | None = self._registry.get(column.oid)
        if info is None:
            return ColumnType(TypeFamily.OTHER, f"oid {column.oid}")

        text = self._text.render(info, column.oid, column.typmod)
        if info.array_oid == column.oid:
            return ColumnType(TypeFamily.ARRAY, text)

        if info.name == PgType.NUMERIC:
            return self._decimal_kind(column.typmod)

        if info.name in (PgType.TIME, PgType.TIMETZ):
            return self._time_kind(info.name, text, column.typmod)

        if info.name in self.ARROW:
            return self._columns.kind(self.of(column))

        family = self.FAMILIES.get(info.name)
        if family is None:
            raise PgArrowError(
                f"column {column.name}: registry type {info.name} has no contract "
                f"family; the type table of PgArrowTypes is incomplete"
            )

        return ColumnType(family, text)

    def covers(self, name: str) -> bool:
        """Есть ли у имени реестра семейство (сторож полноты таблицы)."""
        if name in self.ARROW:
            return True

        if name in (PgType.NUMERIC, PgType.TIME, PgType.TIMETZ):
            return True

        return name in self.FAMILIES

    def _time_kind(self, name: str, text: str, typmod: int) -> ColumnType:
        return ColumnType(
            TypeFamily.TIME,
            text,
            unit=self._unit(typmod),
            zoned=name == PgType.TIMETZ,
        )

    def _decimal_kind(self, typmod: int) -> ColumnType:
        if typmod == self.UNBOUNDED:
            return ColumnType(TypeFamily.DECIMAL, "decimal")

        packed = typmod - self.VARHDRSZ
        precision = packed >> 16
        scale = packed & 0xFFFF

        return ColumnType(
            TypeFamily.DECIMAL,
            f"decimal({precision}, {scale})",
            precision=precision,
            scale=scale,
        )

    def _unit(self, typmod: int) -> TimeUnit:
        """Единица по точности типа: (0) — секунды, (1..3) — миллисекунды,
        (4..6) и без точности — микросекунды."""
        if typmod == 0:
            return TimeUnit.SECOND

        if 0 < typmod <= self.MILLISECONDS:
            return TimeUnit.MILLISECOND

        return TimeUnit.MICROSECOND

    def known(self, column: PgTypedColumn) -> bool:
        """Тип есть в реестре psycopg; пользовательские (enum, составные)
        реестр не знает."""
        return self._registry.get(column.oid) is not None

    def source_type(self, column: PgTypedColumn) -> str:
        info: TypeInfo | None = self._registry.get(column.oid)
        if info is None:
            return ""

        return self._text.render(info, column.oid, column.typmod)

    def char_length(self, column: PgTypedColumn) -> int:
        info: TypeInfo | None = self._registry.get(column.oid)
        if info is None:
            return 0

        return self._text.char_length(info, column.oid, column.typmod)

    def of(self, column: PgTypedColumn) -> pyarrow.DataType:
        info: TypeInfo | None = self._registry.get(column.oid)
        if info is None:
            return pyarrow.large_string()

        if info.array_oid == column.oid:
            return pyarrow.large_string()

        if info.name == PgType.NUMERIC:
            return self._numeric(column.name, column.typmod)

        if info.name in (PgType.TIMESTAMP, PgType.TIMESTAMPTZ):
            return self._timestamp(info.name, column.typmod)

        arrow = self.ARROW.get(info.name)
        if arrow is not None:
            return arrow

        return pyarrow.large_string()

    def _timestamp(self, name: str, typmod: int) -> pyarrow.DataType:
        unit = self._unit(typmod).value
        if name == PgType.TIMESTAMPTZ:
            return pyarrow.timestamp(unit, "UTC")

        return pyarrow.timestamp(unit)

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

    async def get_column_description_from_libpq(
        self,
        query: str,
    ) -> AsyncIterator[PgDescribedColumn]:
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
            # self._ensure_ok(prepared, "preparing", text) проверяет статус ответа.
            # Если сервер вернул ошибку, например «relation does not exist»,
            # она превращается в PgArrowError с текстом запроса.
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
            # self._ensure_ok(described, "describing", text) снова проверяет статус
            # выдает ошибку PgArrowError если не ок
            self._ensure_ok(described, "describing", query)
        except psycopg.Error as exc:
            raise PgArrowError(
                f"describing the statement on postgres failed: {type(exc).__name__}: "
                f"{exc}; query: {query[:200]!r}"
            ) from exc

        for position in range(described.nfields):
            name = described.fname(position)
            if name is None:
                raise PgArrowError(
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

    async def schema(self, query: str) -> pyarrow.Schema:
        """arrow схема из postgres запроса"""
        return self._types.schema(self.get_column_description_from_libpq(query))

    async def stream_into(
        self,
        query: str,
        schema: pyarrow.Schema,
        chunk_bytes: int,
        sink: io.RawIOBase,
    ) -> PgCommandReport:
        """Выборка потоком Arrow по готовой схеме: читатель CSV собирает
        пачки типами схемы, значения вне типа — ошибка чтения."""
        writer = await self._ipc.open_out(sink, schema)
        pipe = BytePipe()
        reader = CsvBatches(schema, chunk_bytes)
        pgquery = (
            PgQueryBuilder()
            .add("copy (")
            .raw_query(query)
            .add(") to stdout (format csv)")
            .build()
        )

        async with self._conn.cursor() as cursor:

            async def produce() -> None:
                try:
                    await self._copy_out(cursor, pgquery.text, pipe, chunk_bytes)
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
                pgquery.text.as_string(self._conn),
                cursor,
            )

    async def _copy_out(
        self,
        cursor: psycopg.AsyncCursor[Any],
        statement: sql.Composed,
        pipe: BytePipe,
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
                f"reading the copy csv stream as arrow failed: "
                f"{type(exc).__name__}: {exc}; a value the reader cannot parse "
                f"(numeric NaN, timestamp infinity) needs ::text or ::float8 in "
                f"the select; the parse block is {self._block} bytes and a row "
                f"must fit into it (raise chunk_bytes for wider rows)"
            ) from exc

        while True:
            try:
                batch = await asyncio.to_thread(self._next, reader)
            except pyarrow.ArrowException as exc:
                raise PgArrowError(
                    f"reading the copy csv stream as arrow failed: "
                    f"{type(exc).__name__}: {exc}; a value the reader cannot "
                    f"parse (numeric NaN, timestamp infinity) needs ::text or "
                    f"::float8 in the select; the parse block is {self._block} "
                    f"bytes and a row must fit into it (raise chunk_bytes for "
                    f"wider rows)"
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

    def _next(
        self, reader: pyarrow.csv.CSVStreamingReader
    ) -> pyarrow.RecordBatch | None:
        # читатель отдаёт поля nullable без metadata; пачка перекладывается в
        # схему контракта (nullable, metadata) без копирования массивов
        try:
            batch = reader.read_next_batch()
        except StopIteration:
            return None

        return pyarrow.RecordBatch.from_arrays(list(batch.columns), schema=self._schema)


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
        """Пачки потока в стейтмент COPY ... FROM STDIN (FORMAT CSV), написанный
        вызывающим; транзакцию держит вызывающий."""
        query = PgQueryBuilder().raw_query(statement).build()

        return await self.copy_query(query, reader)

    async def copy_query(self, query: PgQuery, reader: ArrowReader) -> PgCommandReport:
        """Пачки потока в собранный билдером стейтмент COPY; транзакцию держит
        вызывающий."""
        self._ensure_writable(reader.schema)

        rows = 0
        async with self._conn.cursor() as cursor:
            async with cursor.copy(query.text) as copy:
                async for batch in reader.batches:
                    await copy.write(self._csv(batch))
                    rows += batch.num_rows

            return self._trace.report(
                f"{rows} rows written", query.text.as_string(self._conn), cursor, rows
            )

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
