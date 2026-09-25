"""Перекачка потоком Arrow IPC: ora_arrow_out и ora_arrow_in против
ch_arrow_out и ch_arrow_in (в ловушках — ch_stream_* с FORMAT ArrowStream в
тексте) и pg_arrow_in, насосы соединены трубой ОС и работают одновременно.
Матрица — каждый Oracle из ora_sources против каждого ClickHouse из
ch_sources в обе стороны, в каждый postgres и Greenplum из sources, плюс
круг Oracle -> Oracle.

В отличие от CSV значения не переводятся в текст: числа, float с NaN, время
и двоичные данные едут своими типами Arrow. Приводить в запросе остаётся
только то, что драйвер или сервер в Arrow не отдаёт (INTERVAL, XMLTYPE,
JSON, VECTOR, NUMBER с дробью без точности; DateTime, UUID и Bool ClickHouse).
Отдельные тесты фиксируют ловушки этого пути: регистр имён колонок Oracle,
DateTime как uint32, UUID на 22.12, NVARCHAR2 в однобайтовой базе,
наносекунды TIMESTAMP(9)."""

# ruff: noqa: S608 — стейтменты стенда собираются текстом, как их пишет LLM

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, ClassVar

import pytest

from boba.db.clickhouse.payload import PayloadClickHouse
from boba.db.oracle import OracleQueryError
from boba.db.oracle.payload import PayloadOracle
from boba.pump_stand import (
    ChSource,
    Leg,
    OracleStand,
    OraSource,
    PostgresSide,
    Pumps,
    PumpStand,
)
from boba.pump_stand.compare import (
    BYTES,
    DATETIME,
    EXACT,
    FLOAT,
    FLOAT32,
    JSON,
    NUMBER,
    Report,
    Values,
)
from boba.pump_stand.matrix import Target, compared, first
from boba.pump_stand.oracle import PumpUser

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

STAND = PumpStand.required()
ROWS = 2000
ARRAYSIZE = 97
CHUNK_BYTES = 4096
CH_DATABASE = "pump_arrow"
PG_SCHEMA = "pump_ora_arrow"
NULL_EVERY = 7
CASE_INSENSITIVE = "input_format_arrow_case_insensitive_column_matching = 1"
STRING_AS_STRING = "output_format_arrow_string_as_string = 1"


@dataclass(frozen=True)
class OraColumn:
    """Колонка Oracle для Arrow-пути: тип, заполнение над g, выражение
    выгрузки ora_arrow_out, тип в ClickHouse и опорные выражения обеих
    сторон; native — тип едет в Arrow без приведения и годится для круга
    Oracle -> Oracle."""

    name: str
    ddl: str
    fill: str
    compare: Values
    ch_type: str
    out: str = ""
    ch_ref: str = ""
    ora_ref: str = ""
    approx: float = 0.0
    nullable: bool = False
    min_version: int = 12
    unicode_db: bool = False
    native: bool = True
    unicode_bind: bool = False

    def filled(self) -> str:
        if not self.nullable:
            return f"({self.fill})"

        return f"case when mod(g, {NULL_EVERY}) = 0 then null else ({self.fill}) end"

    def exported(self) -> str:
        expression = self.out
        if not expression:
            expression = self.name

        return f"{expression} as {self.name}"

    def ch_column(self) -> str:
        if self.nullable:
            return f"{self.name} Nullable({self.ch_type})"

        return f"{self.name} {self.ch_type}"


UNICODE = (
    "unistr('\\041a\\0438\\0440\\0438\\043b\\043b\\0438\\0446\\0430 "
    "\\4e2d\\6587 \\d83d\\de42 ')"
)
SECONDS = (
    "extract(day from {c}) * 86400 + extract(hour from {c}) * 3600 "
    "+ extract(minute from {c}) * 60 + extract(second from {c})"
)
MONTHS = "extract(year from {c}) * 12 + extract(month from {c})"

ORA_COLUMNS = (
    OraColumn("id", "number(10) not null", "g", NUMBER, "Int64"),
    OraColumn(
        "n38",
        "number(38)",
        "(mod(g, 1000) - 500) * power(10, 34) + g",
        NUMBER,
        "Decimal(38, 0)",
        nullable=True,
    ),
    OraColumn(
        "n18_4",
        "number(18, 4)",
        "g / 3 - 1000",
        NUMBER,
        "Decimal(18, 4)",
        nullable=True,
    ),
    OraColumn(
        "n38_10",
        "number(38, 10)",
        "g * 1234567890.123456789 - 9999999999999999999999999.9876543210",
        NUMBER,
        "Decimal(38, 10)",
    ),
    OraColumn(
        "nfree",
        "number",
        "g / 7 - 100",
        NUMBER,
        "Decimal(76, 40)",
        out="to_char(nfree, 'TM9')",
        nullable=True,
        native=False,
    ),
    OraColumn("nint", "number", "g * 1000003 - 1000000000", NUMBER, "Int64"),
    OraColumn("nneg", "number(10, -2)", "g * 12345", NUMBER, "Int64"),
    OraColumn(
        "fdbl",
        "float(126)",
        "g / 3",
        FLOAT,
        "Float64",
        ora_ref="to_binary_double(fdbl)",
        native=False,
    ),
    OraColumn(
        "bf",
        "binary_float",
        "case mod(g, 6) when 0 then binary_float_nan "
        "when 1 then binary_float_infinity when 2 then -binary_float_infinity "
        "when 3 then to_binary_float(g) * 1.5e30f else to_binary_float(g / 3) end",
        FLOAT32,
        "Float32",
    ),
    OraColumn(
        "bd",
        "binary_double",
        "case mod(g, 6) when 0 then binary_double_nan "
        "when 1 then binary_double_infinity when 2 then -binary_double_infinity "
        "when 3 then to_binary_double(g) * 1e300d "
        "when 4 then to_binary_double(g) * 1e-300d else to_binary_double(g) / 7 end",
        FLOAT,
        "Float64",
        nullable=True,
    ),
    OraColumn(
        "vc",
        "varchar2(400)",
        "'tab' || chr(9) || g || chr(10) || 'cr' || chr(13) || chr(10) "
        "|| 'back\\slash ''q'' \"dq\", semi;'",
        EXACT,
        "String",
        nullable=True,
    ),
    OraColumn(
        "nvc",
        "nvarchar2(100)",
        f"{UNICODE} || g",
        EXACT,
        "String",
        nullable=True,
        unicode_bind=True,
    ),
    OraColumn(
        "vcu",
        "varchar2(100 char)",
        f"{UNICODE} || g",
        EXACT,
        "String",
        nullable=True,
        unicode_db=True,
    ),
    OraColumn("chr5", "char(5)", "'ab'", EXACT, "String"),
    OraColumn(
        "dt",
        "date",
        "date '2000-01-01' + g * 1.0001 + 1 / 86400",
        DATETIME,
        "DateTime('UTC')",
        nullable=True,
    ),
    OraColumn(
        "ts6",
        "timestamp(6)",
        "timestamp '2000-01-01 00:00:00.123456' "
        "+ numtodsinterval(g * 3661.000001, 'second')",
        DATETIME,
        "DateTime64(6, 'UTC')",
        nullable=True,
    ),
    OraColumn(
        "ts9",
        "timestamp(9)",
        "timestamp '2000-01-01 00:00:00.123456789' "
        "+ numtodsinterval(g + g * 7 / 1000000000, 'second')",
        EXACT,
        "DateTime64(9, 'UTC')",
        out="to_char(ts9, 'yyyy-mm-dd hh24:mi:ss.ff9')",
        ch_ref="toString(ts9)",
        ora_ref="to_char(ts9, 'yyyy-mm-dd hh24:mi:ss.ff9')",
        native=False,
    ),
    OraColumn(
        "tstz",
        "timestamp(6) with time zone",
        "from_tz(timestamp '2020-01-01 00:00:00.5' "
        "+ numtodsinterval(g * 3600.25, 'second'), "
        "case mod(g, 3) when 0 then '+03:00' when 1 then '-05:30' else '+00:00' end)",
        DATETIME,
        "DateTime64(6, 'UTC')",
        out="sys_extract_utc(tstz)",
        ora_ref="sys_extract_utc(tstz)",
        nullable=True,
        native=False,
    ),
    OraColumn(
        "iym",
        "interval year(4) to month",
        "numtoyminterval(g - 1000, 'month')",
        NUMBER,
        "Int32",
        out=MONTHS.format(c="iym"),
        ora_ref=MONTHS.format(c="iym"),
        nullable=True,
        native=False,
    ),
    OraColumn(
        "ids",
        "interval day(6) to second(6)",
        "numtodsinterval(g * 3661.123456 - 3000000, 'second')",
        NUMBER,
        "Decimal(18, 6)",
        out=f"cast({SECONDS.format(c='ids')} as number(18, 6))",
        ora_ref=SECONDS.format(c="ids"),
        nullable=True,
        native=False,
    ),
    OraColumn(
        "rwb",
        "raw(2000)",
        "utl_raw.concat(hextoraw('00FF0A0D22'), "
        "utl_raw.copies(standard_hash(to_char(g), 'SHA256'), 60))",
        BYTES,
        "String",
        ch_ref="hex(rwb)",
        nullable=True,
    ),
    OraColumn(
        "bool",
        "boolean",
        "mod(g, 2) = 0",
        EXACT,
        "Bool",
        nullable=True,
        min_version=23,
    ),
    OraColumn(
        "vec",
        "vector(3, float32)",
        "to_vector('[' || g || ', 1.5, -2.25]', 3, float32)",
        EXACT,
        "String",
        out="from_vector(vec)",
        ora_ref="from_vector(vec)",
        min_version=23,
        native=False,
    ),
    OraColumn(
        "cl",
        "clob",
        "to_clob(rpad('a', 3000, 'b')) || chr(10) || '\"q\",;' "
        "|| to_clob(rpad('c', 3000, 'd')) || g",
        EXACT,
        "String",
        nullable=True,
    ),
    OraColumn("bl", "blob", "null", BYTES, "String", ch_ref="hex(bl)", nullable=True),
    OraColumn(
        "jsc",
        "clob check (jsc is json)",
        '\'{"id": \' || g || \', "s": "x\\"y\\u043a\\u043e", '
        '"a": [1, 2.5, null, true], "o": {"k": {"n": -1e-3}}}\'',
        JSON,
        "String",
        nullable=True,
    ),
    OraColumn(
        "js",
        "json",
        'json(\'{"id": \' || g || \', "a": [1, 2.5, null, true], "o": {"k": "v"}}\')',
        JSON,
        "String",
        out="json_serialize(js returning clob)",
        ora_ref="json_serialize(js returning clob)",
        nullable=True,
        min_version=21,
        native=False,
    ),
    OraColumn(
        "xml",
        "xmltype",
        "xmltype('<r id=\"' || g || '\"><v>x&amp;y</v></r>')",
        EXACT,
        "String",
        out="xmlserialize(document xml as clob)",
        ora_ref="xmlserialize(document xml as clob)",
        nullable=True,
        native=False,
    ),
)

"""LOB-колонки (CLOB, BLOB, JSON, XMLTYPE) стоят последними: Oracle не
принимает длинный bind после LOB-колонки в одном insert (ORA-24816), а
ora_arrow_in идёт по порядку полей схемы."""

BLOB_HEX = (
    "to_clob(rawtohex(dbms_lob.substr(bl, 2000, 1))) "
    "|| to_clob(rawtohex(dbms_lob.substr(bl, 2000, 2001)))"
)
PG_TARGETS: Mapping[str, Target] = {
    "id": Target("bigint"),
    "n38": Target("numeric(38)"),
    "n18_4": Target("numeric(18,4)"),
    "n38_10": Target("numeric(38,10)"),
    "nfree": Target("numeric"),
    "nint": Target("bigint"),
    "nneg": Target("bigint"),
    "fdbl": Target("double precision"),
    "bf": Target("real"),
    "bd": Target("double precision"),
    "vc": Target("text"),
    "nvc": Target("text"),
    "vcu": Target("text"),
    "chr5": Target("char(5)"),
    "dt": Target("timestamp(0)"),
    "ts6": Target("timestamp(6)"),
    "ts9": Target("text"),
    "tstz": Target("timestamp(6)"),
    "iym": Target("integer"),
    "ids": Target("numeric(18,6)"),
    "rwb": Target(
        "bytea",
        out="case when rwb is not null then '\\x' || rawtohex(rwb) end",
        ref="encode(rwb, 'hex')",
    ),
    "bool": Target("boolean"),
    "vec": Target("text"),
    "cl": Target("text"),
    "bl": Target(
        "bytea",
        out=f"case when bl is not null then to_clob('\\x') || {BLOB_HEX} end",
        ref="encode(bl, 'hex')",
    ),
    "jsc": Target("text"),
    "js": Target("text"),
    "xml": Target("text"),
}
"""Приёмник postgres для каждой колонки Oracle: тип, выражение выгрузки и
опорное выражение postgres; выражение и опорное Oracle по умолчанию — из
самой колонки (те же, что для ClickHouse). Двоичные едут hex-текстом с
префиксом \\x, потому что CSV байтов не несёт."""

BLOB_FILL = (
    "declare\n"
    "  b blob;\n"
    "begin\n"
    f"  for r in (select id from arr where mod(id, {NULL_EVERY}) <> 0) loop\n"
    "    dbms_lob.createtemporary(b, true);\n"
    "    for i in 1 .. 3 loop\n"
    "      dbms_lob.writeappend(b, 1000, utl_raw.copies(utl_raw.concat("
    "hextoraw('00FF'), utl_raw.cast_to_raw(lpad(r.id * 10 + i, 8, '0'))), 100));\n"
    "    end loop;\n"
    "    update arr set bl = b where id = r.id;\n"
    "    dbms_lob.freetemporary(b);\n"
    "  end loop;\n"
    "end;"
)


@dataclass(frozen=True)
class ChColumn:
    """Колонка ClickHouse для пути ClickHouse -> Oracle: тип, заполнение над n,
    выражение выгрузки в ArrowStream, тип в Oracle, опорные выражения."""

    name: str
    ch_type: str
    fill: str
    ora_type: str
    compare: Values
    out: str = ""
    ch_ref: str = ""
    ora_ref: str = ""
    unicode_db: bool = False

    def exported(self) -> str:
        expression = self.out
        if not expression:
            expression = self.name

        return f"{expression} as {self.name}"


CH_COLUMNS = (
    ChColumn("id", "Int64", "toInt64(n)", "number(19)", NUMBER),
    ChColumn(
        "u64", "UInt64", "toUInt64(18446744073709551615 - n)", "number(20)", NUMBER
    ),
    ChColumn("d18", "Decimal(18, 4)", "toDecimal64(n, 4) / 3", "number(18, 4)", NUMBER),
    ChColumn(
        "d38", "Decimal(38, 10)", "toDecimal128(n, 10) / 7", "number(38, 10)", NUMBER
    ),
    ChColumn("f32", "Float32", "toFloat32(n) / 3", "binary_float", FLOAT32),
    ChColumn(
        "f64",
        "Float64",
        "multiIf(n % 5 = 0, nan, n % 5 = 1, inf, n % 5 = 2, -inf, n / 7)",
        "binary_double",
        FLOAT,
    ),
    ChColumn(
        "s",
        "String",
        "concat('tab\\t', toString(n), '\\nnew \\'q\\' \"dq\", ко 中文 🙂')",
        "nvarchar2(200)",
        EXACT,
        unicode_db=True,
    ),
    ChColumn("sa", "String", "concat('ascii ', toString(n))", "varchar2(50)", EXACT),
    ChColumn(
        "ns",
        "Nullable(String)",
        "if(n % 7 = 0, NULL, toString(n))",
        "varchar2(20)",
        EXACT,
    ),
    ChColumn(
        "dt",
        "Date",
        "toDate('1970-01-01') + n",
        "date",
        DATETIME,
        out="toDate32(dt)",
    ),
    ChColumn("d32", "Date32", "toDate32('1901-01-01') + n * 20", "date", DATETIME),
    ChColumn(
        "dtm",
        "DateTime('UTC')",
        "toDateTime('2000-01-01 00:00:00', 'UTC') + n * 3601",
        "date",
        DATETIME,
        out="toDateTime64(dtm, 0, 'UTC')",
    ),
    ChColumn(
        "dt64",
        "DateTime64(6, 'UTC')",
        "toDateTime64('2000-01-01 00:00:00.123456', 6, 'UTC') + n * 3661.000001",
        "timestamp(6)",
        DATETIME,
    ),
    ChColumn(
        "dt9",
        "DateTime64(9, 'UTC')",
        "toDateTime64('2000-01-01 00:00:00.123456789', 9, 'UTC') + n",
        "timestamp(9)",
        EXACT,
        ch_ref="substring(toString(dt9), 1, 26)",
        ora_ref="to_char(dt9, 'yyyy-mm-dd hh24:mi:ss.ff6')",
    ),
    ChColumn("b", "Bool", "n % 2 = 0", "number(1)", NUMBER, out="toUInt8(b)"),
    ChColumn(
        "u",
        "UUID",
        "generateUUIDv4()",
        "raw(16)",
        BYTES,
        out="hex(u)",
        ch_ref="replaceAll(toString(u), '-', '')",
    ),
    ChColumn(
        "bin",
        "String",
        "concat(unhex('00FF0A0D'), toString(n))",
        "raw(100)",
        BYTES,
        out="hex(bin)",
        ch_ref="hex(bin)",
    ),
)


class Oracle:
    """Сторона Oracle: версия и кодировка, таблица arr под версию, выборки."""

    def __init__(self, source: OraSource) -> None:
        self.source = source
        self.stand = OracleStand(source)
        self.version = 0
        self.unicode = False

    async def connect(self) -> None:
        self.version = await self.stand.version()
        rows = await self._select_admin(
            "select value from nls_database_parameters "
            "where parameter = 'NLS_CHARACTERSET'"
        )
        self.unicode = rows[0][0] == "AL32UTF8"

    @property
    def owner(self) -> Any:
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

    def ch_columns(self) -> list[ChColumn]:
        chosen: list[ChColumn] = []
        for column in CH_COLUMNS:
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
        await self.stand.run((f"create table arr ({ddl})",))
        await self.stand.run(
            (
                f"insert into arr ({names}) select {filled} "
                "from (select level as g from dual connect by level <= :n)",
            ),
            {"n": rows},
        )
        await self.stand.run((BLOB_FILL,))

    async def create(self, table: str, columns: Sequence[str]) -> None:
        await self.stand.run((f"create table {table} ({', '.join(columns)})",))

    async def select(self, table: str, expressions: Sequence[str]) -> list[Any]:
        payload = PayloadOracle(self.stand.owner)
        async with (
            payload.opened() as conn,
            payload.rows(
                conn,
                f"select {', '.join(expressions)} from {PumpUser.NAME}.{table} "
                "order by id",
            ) as stream,
        ):
            return [row async for row in stream.blocks]

    async def drop(self) -> None:
        await self.stand.drop()

    async def _select_admin(self, text: str) -> list[Any]:
        payload = PayloadOracle(self.source.admin)
        async with payload.opened() as conn, payload.rows(conn, text) as stream:
            return [row async for row in stream.blocks]


class ClickHouse:
    """Сторона ClickHouse: база под обе стороны, выборки."""

    def __init__(self, source: ChSource) -> None:
        self.source = source
        self.major = 0

    async def connect(self) -> None:
        async with PayloadClickHouse.opened_config(self.source.admin) as client:
            result = await client.query("select version()")

        release, *_ = str(result.result_rows[0][0]).split(".")
        self.major = int(release)

    async def recreate(self) -> None:
        async with PayloadClickHouse.opened_config(self.source.admin) as client:
            await client.command(f"drop database if exists {CH_DATABASE}")
            await client.command(f"create database {CH_DATABASE}")

    async def command(self, text: str) -> None:
        async with PayloadClickHouse.opened_config(self.source.admin) as client:
            await client.command(text)

    async def select(self, table: str, expressions: Sequence[str]) -> list[Any]:
        async with PayloadClickHouse.opened_config(self.source.admin) as client:
            result = await client.query(
                f"select {', '.join(expressions)} from {CH_DATABASE}.{table} "
                "order by id"
            )

        return list(result.result_rows)

    async def drop(self) -> None:
        await self.command(f"drop database if exists {CH_DATABASE}")


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


@pytest.fixture(scope="module", params=STAND.demo_clickhouse(), ids=lambda s: s.name)
async def clickhouse(request: Any) -> AsyncIterator[ClickHouse]:
    side = ClickHouse(request.param)
    await side.connect()
    await side.recreate()
    yield side
    await side.drop()


class TestOracleToClickHouse:
    async def test_every_oracle_type_lands(
        self, oracle: Oracle, clickhouse: ClickHouse
    ) -> None:
        columns = oracle.columns()
        ddl = ", ".join(c.ch_column() for c in columns)
        await clickhouse.command(f"drop table if exists {CH_DATABASE}.dst")
        await clickhouse.command(
            f"create table {CH_DATABASE}.dst ({ddl}) engine = MergeTree order by id"
        )
        exported = ", ".join(c.exported() for c in columns)

        pumps = Pumps(clickhouse=clickhouse.source.admin, oracle=oracle.owner)
        chained = await pumps.chain(
            Leg(
                "ora_arrow_out",
                {"sql": f"select {exported} from {PumpUser.NAME}.arr order by id"},
            ),
            Leg(
                "ch_arrow_in",
                {
                    "sql": f"insert into {CH_DATABASE}.dst "
                    f"settings {CASE_INSENSITIVE} format ArrowStream",
                    "chunk_bytes": CHUNK_BYTES,
                },
            ),
        )
        assert chained.in_report == f"{ROWS} rows written"

        expected = await oracle.select("arr", [_or(c.ora_ref, c.name) for c in columns])
        landed = await clickhouse.select(
            "dst", [_or(c.ch_ref, c.name) for c in columns]
        )
        report = Report()
        ids = _column(expected, 0)
        for position, column in enumerate(columns):
            report.compare(
                column.name,
                column.compare,
                column.approx,
                ids,
                _column(expected, position),
                _column(landed, position),
            )

        assert not report.mismatches, report.render()


class TestClickHouseToOracle:
    async def test_every_clickhouse_type_lands(
        self, oracle: Oracle, clickhouse: ClickHouse
    ) -> None:
        columns = oracle.ch_columns()
        ch_ddl = ", ".join(f"{c.name} {c.ch_type}" for c in columns)
        filled = ", ".join(f"({c.fill}) as {c.name}" for c in columns)
        await clickhouse.command(f"drop table if exists {CH_DATABASE}.src")
        await clickhouse.command(
            f"create table {CH_DATABASE}.src ({ch_ddl}) engine = MergeTree order by id"
        )
        await clickhouse.command(
            f"insert into {CH_DATABASE}.src select {filled} "
            f"from (select number as n from numbers(1, {ROWS}))"
        )
        await oracle.create("landed", [f"{c.name} {c.ora_type}" for c in columns])
        exported = ", ".join(c.exported() for c in columns)

        pumps = Pumps(clickhouse=clickhouse.source.admin, oracle=oracle.owner)
        try:
            chained = await pumps.chain(
                Leg(
                    "ch_arrow_out",
                    {
                        "sql": f"select {exported} from {CH_DATABASE}.src order by id "
                        f"settings {STRING_AS_STRING}",
                        "chunk_bytes": CHUNK_BYTES,
                    },
                ),
                Leg("ora_arrow_in", {"table": "landed", "chunk_bytes": CHUNK_BYTES}),
            )
            assert chained.in_report == f"{ROWS} rows written into landed"

            expected = await clickhouse.select(
                "src", [_or(c.ch_ref, c.name) for c in columns]
            )
            landed = await oracle.select(
                "landed", [_or(c.ora_ref, c.name) for c in columns]
            )
        finally:
            await oracle.stand.run(("drop table landed purge",))

        report = Report()
        ids = _column(expected, 0)
        for position, column in enumerate(columns):
            report.compare(
                column.name,
                column.compare,
                0.0,
                ids,
                _column(expected, position),
                _column(landed, position),
            )

        assert not report.mismatches, report.render()


class TestOracleToOracle:
    async def test_native_types_survive_the_circle(self, oracle: Oracle) -> None:
        columns: list[OraColumn] = []
        for column in oracle.columns():
            if not column.native:
                continue

            if column.unicode_bind and not oracle.unicode:
                # bind строки идёт в кодировке базы: юникод NVARCHAR2 в
                # однобайтовой базе через ora_arrow_in не доезжает
                continue

            columns.append(column)

        names = ", ".join(c.name for c in columns)
        await oracle.create("circle", [f"{c.name} {c.ddl}" for c in columns])
        pumps = Pumps(oracle=oracle.owner)
        try:
            chained = await pumps.chain(
                Leg(
                    "ora_arrow_out",
                    {"sql": f"select {names} from {PumpUser.NAME}.arr order by id"},
                ),
                Leg("ora_arrow_in", {"table": "circle", "chunk_bytes": CHUNK_BYTES}),
            )
            assert chained.in_report == f"{ROWS} rows written into circle"

            expected = await oracle.select("arr", [c.name for c in columns])
            landed = await oracle.select("circle", [c.name for c in columns])
        finally:
            await oracle.stand.run(("drop table circle purge",))

        report = Report()
        ids = _column(expected, 0)
        for position, column in enumerate(columns):
            report.compare(
                column.name,
                column.compare,
                0.0,
                ids,
                _column(expected, position),
                _column(landed, position),
            )

        assert not report.mismatches, report.render()


@pytest.fixture(scope="module", params=STAND.sources, ids=lambda s: s.name)
async def postgres(request: Any) -> AsyncIterator[PostgresSide]:
    side = PostgresSide(request.param, PG_SCHEMA)
    await side.connect()
    await side.recreate_schema()
    yield side
    await side.drop()


class TestOracleToPostgres:
    async def test_every_oracle_type_lands(
        self, oracle: Oracle, postgres: PostgresSide
    ) -> None:
        """Имена из Oracle заглавные, а колонки postgres строчные: алиас в
        кавычках даёт имя поля схемы как в приёмнике."""
        columns: list[OraColumn] = []
        targets: list[Target] = []
        for column in oracle.columns():
            target = PG_TARGETS.get(column.name)
            if target is None:
                continue

            columns.append(column)
            targets.append(target)

        table = "from_" + oracle.source.name.replace("-", "_").replace(".", "_")
        await postgres.create(
            table, [f"{c.name} {t.type}" for c, t in zip(columns, targets, strict=True)]
        )
        listed = ", ".join(
            f'{first(t.out, first(c.out, c.name))} as "{c.name}"'
            for c, t in zip(columns, targets, strict=True)
        )
        pumps = Pumps(postgres=postgres.profile, oracle=oracle.owner)
        chained = await pumps.chain(
            Leg(
                "ora_arrow_out",
                {"sql": f"select {listed} from {PumpUser.NAME}.arr order by id"},
            ),
            Leg(
                "pg_arrow_in",
                {"table": f"{PG_SCHEMA}.{table}", "chunk_bytes": CHUNK_BYTES},
            ),
        )
        assert chained.in_report == f"{ROWS} rows written into {PG_SCHEMA}.{table}"

        expected = await oracle.select(
            "arr",
            [
                first(t.src_ref, first(c.ora_ref, c.name))
                for c, t in zip(columns, targets, strict=True)
            ],
        )
        landed = await postgres.select(
            table, [first(t.ref, c.name) for c, t in zip(columns, targets, strict=True)]
        )
        tolerances: list[float] = []
        for column in columns:
            tolerance = column.approx
            if column.compare is FLOAT:
                tolerance = max(tolerance, postgres.double_tolerance())

            tolerances.append(tolerance)

        report = compared(
            [c.name for c in columns],
            [c.compare for c in columns],
            tolerances,
            expected,
            landed,
        )

        assert not report.mismatches, report.render()


class TestTraps:
    """Ловушки Arrow-пути на одной таблице, один Oracle на все версии
    ClickHouse."""

    PROBE: ClassVar[str] = (
        f"select id, n18_4, vc from {PumpUser.NAME}.arr where id <= 3 order by id"
    )

    async def test_uppercase_oracle_names_need_case_insensitive_matching(
        self, oracle: Oracle, clickhouse: ClickHouse
    ) -> None:
        """Oracle отдаёт имена колонок заглавными. ClickHouse 22.12 без
        настройки отказывает, новые версии молча пишут значения по умолчанию;
        с input_format_arrow_case_insensitive_column_matching всё сходится."""
        if oracle.source.name != STAND.ora_sources[-1].name:
            pytest.skip("one oracle is enough for this trap")

        await clickhouse.command(f"drop table if exists {CH_DATABASE}.names")
        await clickhouse.command(
            f"create table {CH_DATABASE}.names (id Int64, n18_4 Decimal(18, 4), "
            "vc Nullable(String)) engine = Memory"
        )
        pumps = Pumps(clickhouse=clickhouse.source.admin, oracle=oracle.owner)
        plain = Leg(
            "ch_stream_in",
            {
                "sql": f"insert into {CH_DATABASE}.names format ArrowStream",
                "chunk_bytes": CHUNK_BYTES,
            },
        )
        matched = Leg(
            "ch_stream_in",
            {
                "sql": f"insert into {CH_DATABASE}.names "
                f"settings {CASE_INSENSITIVE} format ArrowStream",
                "chunk_bytes": CHUNK_BYTES,
            },
        )
        source = Leg("ora_arrow_out", {"sql": self.PROBE})

        if clickhouse.major < 23:
            with pytest.raises(Exception, match="THERE_IS_NO_COLUMN"):
                await pumps.chain(source, plain)
        else:
            await pumps.chain(source, plain)
            silent = await clickhouse.select("names", ["id", "n18_4", "vc"])
            assert [tuple(row) for row in silent] == [(0, 0, None)] * 3
            await clickhouse.command(f"truncate table {CH_DATABASE}.names")

        await pumps.chain(source, matched)
        landed = await clickhouse.select("names", ["id"])
        assert [row[0] for row in landed] == [1, 2, 3]

    async def test_clickhouse_datetime_is_uint32_in_arrow(
        self, oracle: Oracle, clickhouse: ClickHouse
    ) -> None:
        """DateTime ClickHouse уходит в Arrow числом секунд (uint32), и Oracle
        не кладёт число в DATE; toDateTime64(col, 0) даёт настоящий timestamp."""
        if oracle.source.name != STAND.ora_sources[-1].name:
            pytest.skip("one oracle is enough for this trap")

        await oracle.create("moments", ["id number(10)", "dtm date"])
        pumps = Pumps(clickhouse=clickhouse.source.admin, oracle=oracle.owner)
        select = "select toInt64(1) as id, {dtm} as dtm format ArrowStream"
        into = Leg("ora_arrow_in", {"table": "moments", "chunk_bytes": CHUNK_BYTES})
        try:
            with pytest.raises(OracleQueryError, match="ORA-00932"):
                await pumps.chain(
                    Leg(
                        "ch_stream_out",
                        {
                            "sql": select.format(
                                dtm="toDateTime('2024-02-29 13:14:15', 'UTC')"
                            ),
                            "chunk_bytes": CHUNK_BYTES,
                        },
                    ),
                    into,
                )

            await pumps.chain(
                Leg(
                    "ch_stream_out",
                    {
                        "sql": select.format(
                            dtm="toDateTime64('2024-02-29 13:14:15', 0, 'UTC')"
                        ),
                        "chunk_bytes": CHUNK_BYTES,
                    },
                ),
                into,
            )
            landed = await oracle.select(
                "moments", ["id", "to_char(dtm, 'yyyy-mm-dd hh24:mi:ss')"]
            )
        finally:
            await oracle.stand.run(("drop table moments purge",))

        assert landed[0][1] == "2024-02-29 13:14:15"
