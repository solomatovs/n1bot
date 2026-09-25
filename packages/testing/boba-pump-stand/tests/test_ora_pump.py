"""Перекачка Oracle -> postgres и Oracle -> ClickHouse насосом ora_csv_out,
соединённым трубой ОС с pg_stream_in или ch_stream_in: оба насоса работают
одновременно, как в графе workflow. Матрица — каждый Oracle из ora_sources
против каждого postgres и Greenplum из sources и каждого ClickHouse из
ch_sources.

Таблица Oracle несёт все семейства типов: NUMBER разной точности (в том
числе без точности, с отрицательным масштабом и FLOAT), BINARY_FLOAT и
BINARY_DOUBLE с NaN и бесконечностями, VARCHAR2 со спецсимволами, NVARCHAR2
и NCLOB с юникодом вне BMP, CHAR, CLOB, DATE (и с краями 0001 и 9999),
TIMESTAMP до наносекунд, с часовым поясом и локальным, INTERVAL обоих видов
со знаком, RAW, BLOB больше 2000 байт, JSON (CLOB IS JSON и родной тип),
XMLTYPE, BOOLEAN и VECTOR. Набор колонок зависит от версии и кодировки базы.

У каждой колонки записано, каким выражением её выгружает Oracle под
конкретный приёмник, каким типом она ложится в приёмник и как сверяется: на
каждой стороне свое опорное выражение, значения сравниваются поколоночно
после приведения к одному виду. Так видно, где тип едет без потерь, а где
преобразование обязан сделать запрос. Допуск есть только у float в
ClickHouse без precise_float_parsing (22.x): текстовый разбор теряет ulp."""

# ruff: noqa: S608 — стейтменты стенда собираются текстом, как их пишет LLM

from __future__ import annotations

import json
import math
import struct
import uuid
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any, ClassVar

import pytest

from boba.db.clickhouse.payload import PayloadClickHouse
from boba.db.oracle.payload import PayloadOracle
from boba.db.postgres import AsyncPostgresPool
from boba.pump_stand import (
    ChSource,
    OracleStand,
    OraSource,
    PgSource,
    Pumps,
    PumpStand,
)
from boba.pump_stand.oracle import PumpUser

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

STAND = PumpStand.required()
ROWS = 2000
ARRAYSIZE = 97
"""Пачка Arrow выгрузки: мелкая, чтобы границы пачек и порций трубы резали
строки в разных местах."""
CHUNK_BYTES = 4096
PG_SCHEMA = "pump_ora"
CH_DATABASE = "pump_ora"
ORA_TABLE = f"{PumpUser.NAME}.src"
NULL_EVERY = 7
"""Nullable-колонки пусты в строках, где id делится на это число."""


class Values:
    """Сверка значений колонки как есть: база для видов, которым нужно
    привести значение перед сравнением. Наследники переопределяют of и same."""

    def canon(self, value: Any) -> Any:
        if value is None:
            return None

        return self.of(value)

    def of(self, value: Any) -> Any:
        return value

    def same(self, left: Any, right: Any, tolerance: float) -> bool:
        return left == right


class Numbers(Values):
    """Число любого драйвера — Decimal по его тексту: float, int и Decimal
    одного значения совпадают."""

    def of(self, value: Any) -> Any:
        return Decimal(str(value))


class Floats(Values):
    """float с NaN и бесконечностями; tolerance — относительный допуск."""

    def of(self, value: Any) -> Any:
        return float(value)

    def same(self, left: Any, right: Any, tolerance: float) -> bool:
        if left is None or right is None:
            return left is right

        if math.isnan(left):
            return math.isnan(right)

        if left == right:
            return True

        if math.isinf(left):
            return False

        return abs(left - right) <= abs(left) * tolerance


class Floats32(Floats):
    """BINARY_FLOAT: обе стороны приводятся к ближайшему float32 — postgres
    печатает real кратчайшим текстом float32, а не его значением в double."""

    def of(self, value: Any) -> Any:
        packed = struct.pack("f", float(value))
        (single,) = struct.unpack("f", packed)

        return single


class Moments(Values):
    """Дата и время как наивное UTC: aware приводится к UTC, date — к полуночи."""

    def of(self, value: Any) -> Any:
        if isinstance(value, datetime):
            return self._naive(value)

        if isinstance(value, date):
            return datetime(value.year, value.month, value.day)

        raise AssertionError(f"not a date or datetime: {value!r}")

    def _naive(self, value: datetime) -> datetime:
        if value.tzinfo is None:
            return value

        return value.astimezone(UTC).replace(tzinfo=None)


class Uuids(Values):
    """UUID из байтов RAW(16), объекта драйвера или текста."""

    def of(self, value: Any) -> Any:
        if isinstance(value, uuid.UUID):
            return value

        if isinstance(value, bytes):
            return uuid.UUID(bytes=value)

        return uuid.UUID(str(value))


class Hexes(Values):
    """Байты как шестнадцатеричный текст в нижнем регистре: bytes драйвера или
    hex() ClickHouse."""

    def of(self, value: Any) -> Any:
        if isinstance(value, bytes | bytearray | memoryview):
            return bytes(value).hex()

        return str(value).lower()


class Documents(Values):
    """JSON как разобранное значение: текст разбирается, dict драйвера — как есть."""

    def of(self, value: Any) -> Any:
        if isinstance(value, str):
            return json.loads(value)

        return value


class Vectors(Values):
    """VECTOR Oracle, real[] postgres и Array ClickHouse — список float."""

    def of(self, value: Any) -> Any:
        return [float(item) for item in value]


EXACT = Values()
NUMBER = Numbers()
FLOAT = Floats()
FLOAT32 = Floats32()
DATETIME = Moments()
UUID = Uuids()
BYTES = Hexes()
JSON = Documents()
VECTOR = Vectors()


@dataclass(frozen=True)
class PgSide:
    """Колонка в postgres: тип, выражение выгрузки Oracle под postgres, опорные
    выражения postgres и Oracle; пустое выражение — имя колонки."""

    type: str
    out: str = ""
    ref: str = ""
    ora_ref: str = ""


@dataclass(frozen=True)
class ChSide:
    """Колонка в ClickHouse: тип, тип в структуре input() и выражение над ним,
    выражение выгрузки Oracle, опорные выражения ClickHouse и Oracle; approx —
    float, который без precise_float_parsing сверяется с допуском."""

    type: str
    input: str = ""
    inbound: str = ""
    out: str = ""
    ref: str = ""
    ora_ref: str = ""
    approx: float = 0.0


@dataclass(frozen=True)
class OraColumn:
    """Колонка Oracle: тип, выражение заполнения над g (номер строки),
    минимальная версия сервера, нужен ли юникодный NLS_CHARACTERSET, опорное
    выражение Oracle и стороны приёмников; None — приёмник колонку не берёт."""

    name: str
    ddl: str
    fill: str
    compare: Values
    pg: PgSide | None
    ch: ChSide | None
    nullable: bool = False
    min_version: int = 12
    unicode_db: bool = False
    ref: str = ""

    def filled(self) -> str:
        if not self.nullable:
            return f"({self.fill})"

        return f"case when mod(g, {NULL_EVERY}) = 0 then null else ({self.fill}) end"


JSON_TYPE = "JSON"
TM9 = "'TM9'"
SECONDS = (
    "extract(day from {c}) * 86400 + extract(hour from {c}) * 3600 "
    "+ extract(minute from {c}) * 60 + extract(second from {c})"
)
MONTHS = "extract(year from {c}) * 12 + extract(month from {c})"
BLOB_HEX = (
    "to_clob(rawtohex(dbms_lob.substr(bl, 2000, 1))) "
    "|| to_clob(rawtohex(dbms_lob.substr(bl, 2000, 2001)))"
)
UNICODE = (
    "unistr('\\041a\\0438\\0440\\0438\\043b\\043b\\0438\\0446\\0430 "
    "\\4e2d\\6587 \\d83d\\de42 ')"
)
FLOAT32_ULP = 2e-7
FLOAT64_ULP = 4e-16

ORA_COLUMNS = (
    OraColumn(
        "id",
        "number(10) not null",
        "g",
        NUMBER,
        PgSide("bigint"),
        ChSide("Int64"),
    ),
    OraColumn(
        "n38",
        "number(38)",
        "(mod(g, 1000) - 500) * power(10, 34) + g",
        NUMBER,
        PgSide("numeric(38)"),
        ChSide("Decimal(38, 0)"),
        nullable=True,
    ),
    OraColumn(
        "n18_4",
        "number(18, 4)",
        "g / 3 - 1000",
        NUMBER,
        PgSide("numeric(18, 4)"),
        ChSide("Decimal(18, 4)"),
        nullable=True,
    ),
    OraColumn(
        "n38_10",
        "number(38, 10)",
        "g * 1234567890.123456789 - 9999999999999999999999999.9876543210",
        NUMBER,
        PgSide("numeric(38, 10)"),
        ChSide("Decimal(38, 10)"),
        nullable=True,
    ),
    OraColumn(
        "nfree",
        "number",
        "g / 7 - 100",
        NUMBER,
        PgSide("numeric", out=f"to_char(nfree, {TM9})"),
        ChSide("Decimal(76, 40)", out=f"to_char(nfree, {TM9})"),
        nullable=True,
    ),
    OraColumn(
        "nint",
        "number",
        "g * 1000003 - 1000000000",
        NUMBER,
        PgSide("bigint"),
        ChSide("Int64"),
    ),
    OraColumn(
        "nneg",
        "number(10, -2)",
        "g * 12345",
        NUMBER,
        PgSide("bigint"),
        ChSide("Int64"),
    ),
    OraColumn(
        "flt",
        "float(126)",
        "g / 3",
        NUMBER,
        PgSide("numeric", out=f"to_char(flt, {TM9})"),
        ChSide("Decimal(76, 40)", out=f"to_char(flt, {TM9})"),
    ),
    OraColumn(
        "fdbl",
        "float(126)",
        "g / 3",
        FLOAT,
        PgSide("double precision", ora_ref="to_binary_double(fdbl)"),
        ChSide(
            "Float64",
            input="String",
            inbound="toFloat64(fdbl)",
            ora_ref="to_binary_double(fdbl)",
            approx=FLOAT64_ULP,
        ),
    ),
    OraColumn(
        "bf",
        "binary_float",
        "case mod(g, 6) when 0 then binary_float_nan "
        "when 1 then binary_float_infinity when 2 then -binary_float_infinity "
        "when 3 then to_binary_float(g) * 1.5e30f else to_binary_float(g / 3) end",
        FLOAT32,
        PgSide("real"),
        ChSide(
            "Float32",
            input="String",
            inbound="toFloat32(toFloat64(bf))",
            approx=FLOAT32_ULP,
        ),
    ),
    OraColumn(
        "bd",
        "binary_double",
        "case mod(g, 6) when 0 then binary_double_nan "
        "when 1 then binary_double_infinity when 2 then -binary_double_infinity "
        "when 3 then to_binary_double(g) * 1e300d "
        "when 4 then to_binary_double(g) * 1e-300d else to_binary_double(g) / 7 end",
        FLOAT,
        PgSide("double precision"),
        ChSide("Float64", input="String", inbound="toFloat64(bd)", approx=FLOAT64_ULP),
        nullable=True,
    ),
    OraColumn(
        "vc",
        "varchar2(400)",
        "'tab' || chr(9) || g || chr(10) || 'cr' || chr(13) || chr(10) "
        "|| 'back\\slash ''q'' \"dq\", semi;'",
        EXACT,
        PgSide("text"),
        ChSide("String"),
        nullable=True,
    ),
    OraColumn(
        "nvc",
        "nvarchar2(100)",
        f"{UNICODE} || g",
        EXACT,
        PgSide("text"),
        ChSide("String"),
        nullable=True,
    ),
    OraColumn(
        "vcu",
        "varchar2(100 char)",
        f"{UNICODE} || g",
        EXACT,
        PgSide("text"),
        ChSide("String"),
        nullable=True,
        unicode_db=True,
    ),
    OraColumn(
        "chr5",
        "char(5)",
        "'ab'",
        EXACT,
        PgSide("char(5)"),
        ChSide("String"),
    ),
    OraColumn(
        "cl",
        "clob",
        "to_clob(rpad('a', 3000, 'b')) || chr(10) || '\"q\",;' "
        "|| to_clob(rpad('c', 3000, 'd')) || g",
        EXACT,
        PgSide("text"),
        ChSide("String"),
        nullable=True,
    ),
    OraColumn(
        "ncl",
        "nclob",
        f"to_nclob({UNICODE} || g)",
        EXACT,
        PgSide("text"),
        ChSide("String"),
        nullable=True,
    ),
    OraColumn(
        "dt",
        "date",
        "date '2000-01-01' + g * 1.0001 + 1 / 86400",
        DATETIME,
        PgSide("timestamp(0)"),
        ChSide("DateTime('UTC')"),
        nullable=True,
    ),
    OraColumn(
        "dtw",
        "date",
        "case mod(g, 3) when 0 then date '0001-01-01' "
        "when 1 then to_date('9999-12-31 23:59:59', 'yyyy-mm-dd hh24:mi:ss') "
        "else date '1850-06-15' + g end",
        EXACT,
        PgSide(
            "timestamp(0)",
            ref="to_char(dtw, 'YYYY-MM-DD HH24:MI:SS')",
            ora_ref="to_char(dtw, 'yyyy-mm-dd hh24:mi:ss')",
        ),
        ChSide("String", ora_ref="to_char(dtw, 'yyyy-mm-dd hh24:mi:ss')"),
    ),
    OraColumn(
        "dd",
        "date",
        "date '1901-01-01' + g * 37",
        DATETIME,
        PgSide("date"),
        ChSide("Date32", out="to_char(dd, 'yyyy-mm-dd')"),
    ),
    OraColumn(
        "ts6",
        "timestamp(6)",
        "timestamp '2000-01-01 00:00:00.123456' "
        "+ numtodsinterval(g * 3661.000001, 'second')",
        DATETIME,
        PgSide("timestamp(6)"),
        ChSide("DateTime64(6, 'UTC')"),
        nullable=True,
    ),
    OraColumn(
        "ts9",
        "timestamp(9)",
        "timestamp '2000-01-01 00:00:00.123456789' "
        "+ numtodsinterval(g + g * 7 / 1000000000, 'second')",
        EXACT,
        PgSide(
            "timestamp(6)",
            out="to_char(cast(ts9 as timestamp(6)), 'yyyy-mm-dd hh24:mi:ss.ff6')",
            ref="to_char(ts9, 'YYYY-MM-DD HH24:MI:SS.US')",
            ora_ref="to_char(cast(ts9 as timestamp(6)), 'yyyy-mm-dd hh24:mi:ss.ff6')",
        ),
        ChSide(
            "DateTime64(9, 'UTC')",
            out="to_char(ts9, 'yyyy-mm-dd hh24:mi:ss.ff9')",
            ref="toString(ts9)",
            ora_ref="to_char(ts9, 'yyyy-mm-dd hh24:mi:ss.ff9')",
        ),
    ),
    OraColumn(
        "tstz",
        "timestamp(6) with time zone",
        "from_tz(timestamp '2020-01-01 00:00:00.5' "
        "+ numtodsinterval(g * 3600.25, 'second'), "
        "case mod(g, 3) when 0 then '+03:00' when 1 then '-05:30' else '+00:00' end)",
        DATETIME,
        PgSide(
            "timestamptz",
            out="to_char(tstz, 'yyyy-mm-dd hh24:mi:ss.ff6tzh:tzm')",
            ref="tstz at time zone 'UTC'",
        ),
        ChSide(
            "DateTime64(6, 'UTC')",
            out="to_char(tstz, 'yyyy-mm-dd hh24:mi:ss.ff6tzh:tzm')",
        ),
        nullable=True,
        ref="sys_extract_utc(tstz)",
    ),
    OraColumn(
        "tsltz",
        "timestamp(6) with local time zone",
        "cast(from_tz(timestamp '2020-06-01 12:00:00' "
        "+ numtodsinterval(g, 'minute'), '+05:00') as timestamp with local time zone)",
        DATETIME,
        PgSide(
            "timestamptz",
            out="to_char(cast(tsltz as timestamp with time zone), "
            "'yyyy-mm-dd hh24:mi:ss.ff6tzh:tzm')",
            ref="tsltz at time zone 'UTC'",
        ),
        ChSide(
            "DateTime64(6, 'UTC')",
            out="to_char(cast(tsltz as timestamp with time zone), "
            "'yyyy-mm-dd hh24:mi:ss.ff6tzh:tzm')",
        ),
        ref="sys_extract_utc(cast(tsltz as timestamp with time zone))",
    ),
    OraColumn(
        "iym",
        "interval year(4) to month",
        "numtoyminterval(g - 1000, 'month')",
        NUMBER,
        PgSide("interval", out="to_char(iym)", ref=MONTHS.format(c="iym")),
        ChSide("Int32", out=MONTHS.format(c="iym")),
        nullable=True,
        ref=MONTHS.format(c="iym"),
    ),
    OraColumn(
        "ids",
        "interval day(6) to second(6)",
        "numtodsinterval(g * 3661.123456 - 3000000, 'second')",
        NUMBER,
        PgSide(
            "interval",
            out=f"to_char({SECONDS.format(c='ids')}, {TM9})",
            ref="extract(epoch from ids)",
        ),
        ChSide("Decimal(18, 6)", out=f"to_char({SECONDS.format(c='ids')}, {TM9})"),
        nullable=True,
        ref=SECONDS.format(c="ids"),
    ),
    OraColumn(
        "rw16",
        "raw(16)",
        "standard_hash(to_char(g), 'MD5')",
        UUID,
        PgSide("uuid", out="rawtohex(rw16)"),
        ChSide("UUID", out="rawtohex(rw16)"),
        nullable=True,
    ),
    OraColumn(
        "rwb",
        "raw(2000)",
        "utl_raw.concat(hextoraw('00FF0A0D22'), "
        "utl_raw.copies(standard_hash(to_char(g), 'SHA256'), 60))",
        BYTES,
        PgSide(
            "bytea",
            out="case when rwb is not null then '\\x' || rawtohex(rwb) end",
        ),
        ChSide(
            "String",
            input="String",
            inbound="unhex(rwb)",
            out="rawtohex(rwb)",
            ref="hex(rwb)",
        ),
        nullable=True,
    ),
    OraColumn(
        "bl",
        "blob",
        "null",
        BYTES,
        PgSide(
            "bytea",
            out=f"case when bl is not null then to_clob('\\x') || {BLOB_HEX} end",
        ),
        ChSide(
            "String",
            input="String",
            inbound="unhex(bl)",
            out=BLOB_HEX,
            ref="hex(bl)",
        ),
        nullable=True,
    ),
    OraColumn(
        "jsc",
        "clob check (jsc is json)",
        '\'{"id": \' || g || \', "s": "x\\"y\\u043a\\u043e", '
        '"a": [1, 2.5, null, true], "o": {"k": {"n": -1e-3}}}\'',
        JSON,
        PgSide(JSON_TYPE),
        ChSide("String"),
        nullable=True,
    ),
    OraColumn(
        "js",
        "json",
        'json(\'{"id": \' || g || \', "a": [1, 2.5, null, true], "o": {"k": "v"}}\')',
        JSON,
        PgSide(JSON_TYPE, out="json_serialize(js returning clob)"),
        ChSide("String", out="json_serialize(js returning clob)"),
        nullable=True,
        min_version=21,
        ref="json_serialize(js returning clob)",
    ),
    OraColumn(
        "xml",
        "xmltype",
        "xmltype('<r id=\"' || g || '\"><v>x&amp;y</v></r>')",
        EXACT,
        PgSide("xml", out="xmlserialize(document xml as clob)", ref="xml::text"),
        ChSide("String", out="xmlserialize(document xml as clob)"),
        nullable=True,
        ref="xmlserialize(document xml as clob)",
    ),
    OraColumn(
        "bool",
        "boolean",
        "mod(g, 2) = 0",
        EXACT,
        PgSide("boolean"),
        ChSide("Bool"),
        nullable=True,
        min_version=23,
    ),
    OraColumn(
        "vec",
        "vector(3, float32)",
        "to_vector('[' || g || ', 1.5, -2.25]', 3, float32)",
        VECTOR,
        PgSide("real[]", out="translate(from_vector(vec), '[]', '{}')"),
        ChSide(
            "Array(Float32)",
            input="String",
            inbound="CAST(JSONExtract(vec, 'Array(Float64)'), 'Array(Float32)')",
            out="from_vector(vec)",
        ),
        min_version=23,
    ),
)

BLOB_FILL = (
    "declare\n"
    "  b blob;\n"
    "begin\n"
    f"  for r in (select id from src where mod(id, {NULL_EVERY}) <> 0) loop\n"
    "    dbms_lob.createtemporary(b, true);\n"
    "    for i in 1 .. 3 loop\n"
    "      dbms_lob.writeappend(b, 1000, utl_raw.copies(utl_raw.concat("
    "hextoraw('00FF'), utl_raw.cast_to_raw(lpad(r.id * 10 + i, 8, '0'))), 100));\n"
    "    end loop;\n"
    "    update src set bl = b where id = r.id;\n"
    "    dbms_lob.freetemporary(b);\n"
    "  end loop;\n"
    "end;"
)
"""BLOB в 3000 байт с нулём и 0xFF: одним SQL-выражением больше 2000 байт
RAW не собрать, поэтому заполняет PL/SQL-блок."""


@dataclass(frozen=True)
class Mismatch:
    """Расхождение колонки: сколько строк и первые примеры (id, Oracle, приёмник)."""

    rows: int
    samples: tuple[tuple[Any, Any, Any], ...]


@dataclass
class Report:
    """Расхождения по колонкам после сверки."""

    mismatches: dict[str, Mismatch] = field(default_factory=dict)

    def compare(  # noqa: PLR0913 — колонка, её вид, допуск и обе стороны
        self,
        name: str,
        compare: Values,
        tolerance: float,
        ids: Sequence[Any],
        oracle: Sequence[Any],
        target: Sequence[Any],
    ) -> None:
        count = 0
        samples: list[tuple[Any, Any, Any]] = []
        for key, left, right in zip(ids, oracle, target, strict=True):
            expected = compare.canon(left)
            landed = compare.canon(right)
            if compare.same(expected, landed, tolerance):
                continue

            count += 1
            if len(samples) < 3:
                samples.append((key, expected, landed))

        if count:
            self.mismatches[name] = Mismatch(count, tuple(samples))

    def render(self) -> str:
        lines: list[str] = []
        for name, mismatch in self.mismatches.items():
            lines.append(
                f"{name}: {mismatch.rows} rows differ, e.g. {mismatch.samples}"
            )

        return "\n".join(lines)


class Oracle:
    """Сторона Oracle: версия и кодировка, таблица src под версию, опорная
    выборка."""

    def __init__(self, source: OraSource) -> None:
        self.source = source
        self.stand = OracleStand(source)
        self.version = 0
        self.unicode = False

    async def connect(self) -> None:
        self.version = await self.stand.version()
        payload = PayloadOracle(self.source.admin)
        async with (
            payload.opened() as conn,
            payload.rows(
                conn,
                "select value from nls_database_parameters "
                "where parameter = 'NLS_CHARACTERSET'",
            ) as stream,
        ):
            rows = [row async for row in stream.blocks]

        self.unicode = rows[0][0] == "AL32UTF8"

    @property
    def owner(self) -> Any:
        """Профиль владельца схемы с мелкой пачкой выгрузки."""
        return self.stand.owner.model_copy(update={"arraysize": ARRAYSIZE})

    def columns(self) -> list[OraColumn]:
        chosen: list[OraColumn] = []
        for column in ORA_COLUMNS:
            if column.min_version > self.version:
                continue

            if column.unicode_db and not self.unicode:
                continue

            chosen.append(column)

        return chosen

    async def recreate(self, rows: int) -> None:
        columns = self.columns()
        ddl = ", ".join(f"{c.name} {c.ddl}" for c in columns)
        names = ", ".join(c.name for c in columns)
        filled = ", ".join(c.filled() for c in columns)
        await self.stand.recreate_user()
        await self.stand.run(
            (f"create table src ({ddl}, constraint src_pk primary key (id))",)
        )
        await self.stand.run(
            (
                f"insert into src ({names}) select {filled} "
                "from (select level as g from dual connect by level <= :n)",
            ),
            {"n": rows},
        )
        await self.stand.run((BLOB_FILL,))

    async def select(self, expressions: Sequence[str]) -> list[Sequence[Any]]:
        payload = PayloadOracle(self.stand.owner)
        async with (
            payload.opened() as conn,
            payload.rows(
                conn, f"select {', '.join(expressions)} from {ORA_TABLE} order by id"
            ) as stream,
        ):
            return [row async for row in stream.blocks]

    async def drop(self) -> None:
        await self.stand.drop()


class Postgres:
    """Сторона postgres: версия, таблица dst под колонки Oracle, опорная выборка."""

    GREENPLUM_6: ClassVar[str] = "Greenplum Database 6"

    def __init__(self, source: PgSource) -> None:
        self.source = source
        self.version = 0
        self.float_tolerance = 0.0

    async def connect(self) -> None:
        async with await AsyncPostgresPool.dedicated(self.source.postgres) as conn:
            cursor = await conn.execute(
                "select current_setting('server_version_num'), version()"
            )
            row = await cursor.fetchone()
            if row is None:
                raise AssertionError("server_version_num returned no row")

        self.version = int(row[0])
        if self.GREENPLUM_6 in row[1]:
            # Greenplum 6 разбирает double порядка 1e-297 с ошибкой в ulp
            self.float_tolerance = FLOAT64_ULP

    def type_of(self, side: PgSide) -> str:
        if side.type != JSON_TYPE:
            return side.type

        if self.version >= 90400:
            return "jsonb"

        if self.version >= 90200:
            return "json"

        return "text"

    async def recreate(self, columns: Sequence[OraColumn]) -> None:
        parts: list[str] = []
        for column in columns:
            if column.pg is None:
                continue

            parts.append(f"{column.name} {self.type_of(column.pg)}")

        async with await AsyncPostgresPool.dedicated(self.source.postgres) as conn:
            await conn.execute(self._q(f"drop schema if exists {PG_SCHEMA} cascade"))
            await conn.execute(self._q(f"create schema {PG_SCHEMA}"))
            await conn.execute(
                self._q(f"create table {PG_SCHEMA}.dst ({', '.join(parts)})")
            )

    async def select(self, expressions: Sequence[str]) -> list[Sequence[Any]]:
        async with await AsyncPostgresPool.dedicated(self.source.postgres) as conn:
            await conn.execute("set extra_float_digits = 3")
            cursor = await conn.execute(
                self._q(
                    f"select {', '.join(expressions)} from {PG_SCHEMA}.dst order by id"
                )
            )
            return list(await cursor.fetchall())

    async def drop(self) -> None:
        async with await AsyncPostgresPool.dedicated(self.source.postgres) as conn:
            await conn.execute(self._q(f"drop schema if exists {PG_SCHEMA} cascade"))

    def _q(self, text: str) -> bytes:
        """psycopg принимает литеральную строку или bytes; текст собран."""
        return text.encode()


class ClickHouse:
    """Сторона ClickHouse: версия, настройки, таблица dst, опорная выборка."""

    def __init__(self, source: ChSource) -> None:
        self.source = source
        self.precise_floats = False

    async def connect(self) -> None:
        async with PayloadClickHouse.opened_config(self.source.admin) as client:
            found = await client.query(
                "select count() from system.settings "
                "where name = 'precise_float_parsing'"
            )

        self.precise_floats = found.result_rows[0][0] == 1

    async def recreate(self, columns: Sequence[OraColumn]) -> None:
        parts: list[str] = []
        for column in columns:
            if column.ch is None:
                continue

            parts.append(f"{column.name} {self.nullable(column, column.ch.type)}")

        async with PayloadClickHouse.opened_config(self.source.admin) as client:
            await client.command(f"drop database if exists {CH_DATABASE}")
            await client.command(f"create database {CH_DATABASE}")
            await client.command(
                f"create table {CH_DATABASE}.dst ({', '.join(parts)}) "
                "engine = MergeTree order by id"
            )

    def nullable(self, column: OraColumn, ch_type: str) -> str:
        if column.nullable:
            return f"Nullable({ch_type})"

        return ch_type

    def insert(self, columns: Sequence[OraColumn]) -> str:
        names: list[str] = []
        structure: list[str] = []
        inbound: list[str] = []
        for column in columns:
            side = column.ch
            if side is None:
                continue

            input_type = side.input
            if not input_type:
                input_type = side.type

            expression = side.inbound
            if not expression:
                expression = column.name

            names.append(column.name)
            structure.append(f"{column.name} {self.nullable(column, input_type)}")
            inbound.append(f"{expression} as {column.name}")

        quoted = ", ".join(structure).replace("'", "''")
        settings = ""
        if self.precise_floats:
            settings = " settings precise_float_parsing = 1"

        return (
            f"insert into {CH_DATABASE}.dst ({', '.join(names)}) "
            f"select {', '.join(inbound)} from input('{quoted}'){settings} format CSV"
        )

    async def select(self, expressions: Sequence[str]) -> list[Sequence[Any]]:
        async with PayloadClickHouse.opened_config(self.source.admin) as client:
            result = await client.query(
                f"select {', '.join(expressions)} from {CH_DATABASE}.dst order by id"
            )

        return list(result.result_rows)

    async def drop(self) -> None:
        async with PayloadClickHouse.opened_config(self.source.admin) as client:
            await client.command(f"drop database if exists {CH_DATABASE}")


def _or(value: str, default: str) -> str:
    if value:
        return value

    return default


def _column(rows: Sequence[Sequence[Any]], position: int) -> list[Any]:
    return [row[position] for row in rows]


@pytest.fixture(scope="module", params=STAND.ora_sources, ids=lambda s: s.name)
async def oracle(request: Any) -> AsyncIterator[Oracle]:
    side = Oracle(request.param)
    await side.connect()
    await side.recreate(ROWS)
    yield side
    await side.drop()


@pytest.fixture(scope="module", params=STAND.sources, ids=lambda s: s.name)
async def postgres(request: Any) -> AsyncIterator[Postgres]:
    side = Postgres(request.param)
    await side.connect()
    yield side
    await side.drop()


@pytest.fixture(scope="module", params=STAND.demo_clickhouse(), ids=lambda s: s.name)
async def clickhouse(request: Any) -> AsyncIterator[ClickHouse]:
    side = ClickHouse(request.param)
    await side.connect()
    yield side
    await side.drop()


class TestOracleToPostgres:
    async def test_every_oracle_type_lands(
        self, oracle: Oracle, postgres: Postgres
    ) -> None:
        columns: list[OraColumn] = []
        for column in oracle.columns():
            if column.pg is not None:
                columns.append(column)

        await postgres.recreate(columns)
        outbound: list[str] = []
        for column in columns:
            side = column.pg
            if side is None:
                continue

            outbound.append(f"{_or(side.out, column.name)} as {column.name}")

        names = ", ".join(c.name for c in columns)
        pumps = Pumps(postgres=postgres.source.postgres, oracle=oracle.owner)
        chained = await pumps.chain(
            "ora_csv_out",
            f"select {', '.join(outbound)} from {ORA_TABLE} order by id",
            "pg_stream_in",
            f"copy {PG_SCHEMA}.dst ({names}) from stdin (format csv)",
            CHUNK_BYTES,
        )
        assert chained.in_report == f"server: COPY {ROWS}"

        ora_refs: list[str] = []
        pg_refs: list[str] = []
        for column in columns:
            side = column.pg
            if side is None:
                continue

            ora_refs.append(_or(side.ora_ref, _or(column.ref, column.name)))
            pg_refs.append(_or(side.ref, column.name))

        expected = await oracle.select(ora_refs)
        landed = await postgres.select(pg_refs)
        ids = _column(expected, 0)
        report = Report()
        for position, column in enumerate(columns):
            report.compare(
                column.name,
                column.compare,
                postgres.float_tolerance,
                ids,
                _column(expected, position),
                _column(landed, position),
            )

        assert not report.mismatches, report.render()


class TestOracleToClickHouse:
    async def test_every_oracle_type_lands(
        self, oracle: Oracle, clickhouse: ClickHouse
    ) -> None:
        columns: list[OraColumn] = []
        for column in oracle.columns():
            if column.ch is not None:
                columns.append(column)

        await clickhouse.recreate(columns)
        outbound: list[str] = []
        for column in columns:
            side = column.ch
            if side is None:
                continue

            outbound.append(f"{_or(side.out, column.name)} as {column.name}")

        pumps = Pumps(clickhouse=clickhouse.source.admin, oracle=oracle.owner)
        chained = await pumps.chain(
            "ora_csv_out",
            f"select {', '.join(outbound)} from {ORA_TABLE} order by id",
            "ch_stream_in",
            clickhouse.insert(columns),
            CHUNK_BYTES,
        )
        assert chained.in_report == f"{ROWS} rows written"

        ora_refs: list[str] = []
        ch_refs: list[str] = []
        tolerances: list[float] = []
        for column in columns:
            side = column.ch
            if side is None:
                continue

            ora_refs.append(_or(side.ora_ref, _or(column.ref, column.name)))
            ch_refs.append(_or(side.ref, column.name))
            tolerance = 0.0
            if not clickhouse.precise_floats:
                tolerance = side.approx

            tolerances.append(tolerance)

        expected = await oracle.select(ora_refs)
        landed = await clickhouse.select(ch_refs)
        ids = _column(expected, 0)
        report = Report()
        for position, column in enumerate(columns):
            report.compare(
                column.name,
                column.compare,
                tolerances[position],
                ids,
                _column(expected, position),
                _column(landed, position),
            )

        assert not report.mismatches, report.render()
