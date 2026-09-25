"""Перекачка из ClickHouse потоком Arrow IPC: ch_arrow_out против pg_arrow_in
(каждый ClickHouse из ch_sources в каждый postgres и Greenplum из sources),
ora_arrow_in (в каждый Oracle из ora_sources) и ch_arrow_in (круг ClickHouse
-> ClickHouse). Насосы соединены трубой ОС и работают одновременно.

Таблица ClickHouse несёт все семейства типов: целые до 256 бит, Decimal до
256 бит, Float с NaN и бесконечностями, String и FixedString с NUL,
LowCardinality, Date и Date32, DateTime и DateTime64 до наносекунд, Enum,
UUID, IPv4/IPv6, Bool, массивы, Tuple, Map, гео-типы и Nullable. У каждой
колонки записано, что писать в select под конкретный приёмник: родной тип
Arrow там, где приёмник его читает, и явный toString или hex там, где нет
(широкие целые, DateTime64(9), Enum, UUID, IP, составные), — так по самим
тестам видно, какие типы едут только строкой."""

# ruff: noqa: S608 — стейтменты стенда собираются текстом, как их пишет LLM

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from typing import Any

import pytest

from boba.pump_stand import (
    ClickHouseSide,
    Leg,
    OracleSide,
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
    NUMBER,
    UUID,
    Report,
    Values,
)
from boba.pump_stand.matrix import (
    Target,
    compared,
    copy_into,
    exported,
    first,
    insert_into,
)

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

STAND = PumpStand.required()
ROWS = 2000
ARRAYSIZE = 97
CHUNK_BYTES = 4096
PG_SCHEMA = "pump_ch_arrow"
CH_DATABASE = "pump_ch_arrow"
STRING_AS_STRING = "output_format_arrow_string_as_string = 1"


@dataclass(frozen=True)
class ChColumn:
    """Колонка ClickHouse: тип, заполнение над n, сверка, стороны приёмников
    (None — приёмник её не берёт), unicode — строка с юникодом (в Oracle с
    однобайтовой базой не доедет), circle_major — с какой версии тип едет в
    ClickHouse родным."""

    name: str
    ch_type: str
    fill: str
    compare: Values
    pg: Target | None
    ora: Target | None
    unicode: bool = False
    circle_major: int = 22
    """Версия ClickHouse, с которой тип выгружается в Arrow родным и читается
    обратно: до неё круг пропускает колонку."""


CH_COLUMNS = (
    ChColumn(
        "id", "Int64", "toInt64(n)", NUMBER, Target("bigint"), Target("number(19)")
    ),
    ChColumn(
        "i8",
        "Int8",
        "toInt8(n % 256 - 128)",
        NUMBER,
        Target("smallint"),
        Target("number(3)"),
    ),
    ChColumn(
        "u8",
        "UInt8",
        "toUInt8(n % 256)",
        NUMBER,
        Target("smallint"),
        Target("number(3)"),
    ),
    ChColumn(
        "i16",
        "Int16",
        "toInt16(n * 21 - 32768)",
        NUMBER,
        Target("integer"),
        Target("number(5)"),
    ),
    ChColumn(
        "u16",
        "UInt16",
        "toUInt16(n * 21)",
        NUMBER,
        Target("integer"),
        Target("number(5)"),
    ),
    ChColumn(
        "i32",
        "Int32",
        "toInt32(n * 700000 - 2147483648)",
        NUMBER,
        Target("bigint"),
        Target("number(10)"),
    ),
    ChColumn(
        "u32",
        "UInt32",
        "toUInt32(n * 1400000)",
        NUMBER,
        Target("bigint"),
        Target("number(10)"),
    ),
    ChColumn(
        "u64",
        "UInt64",
        "toUInt64(18446744073709551615 - n)",
        NUMBER,
        Target("numeric(20)"),
        Target("number(20)"),
    ),
    ChColumn(
        "i128",
        "Int128",
        "toInt128('-170141183460469231731687303715884105728') + toInt128(n)",
        NUMBER,
        Target("numeric(40)", out="toString(i128)"),
        Target("varchar2(45)", out="toString(i128)", src_ref="toString(i128)"),
        circle_major=23,
    ),
    ChColumn(
        "u256",
        "UInt256",
        "toUInt256('1157920892373161954235709850086879078532699846656405640394575840"
        "07913129639935') - toUInt256(n)",
        NUMBER,
        Target("numeric(80)", out="toString(u256)"),
        Target("varchar2(80)", out="toString(u256)", src_ref="toString(u256)"),
        circle_major=23,
    ),
    ChColumn(
        "f32",
        "Float32",
        "toFloat32(n) / 3",
        FLOAT32,
        Target("real"),
        Target("binary_float"),
    ),
    ChColumn(
        "f64",
        "Float64",
        "multiIf(n % 5 = 0, nan, n % 5 = 1, inf, n % 5 = 2, -inf, toFloat64(n) / 7)",
        FLOAT,
        Target("double precision"),
        Target("binary_double"),
    ),
    ChColumn(
        "d32",
        "Decimal32(4)",
        "toDecimal32(n, 4) / 3",
        NUMBER,
        Target("numeric(9,4)"),
        Target("number(9,4)"),
    ),
    ChColumn(
        "d64",
        "Decimal64(8)",
        "toDecimal64(n, 8) / 7",
        NUMBER,
        Target("numeric(18,8)"),
        Target("number(18,8)"),
    ),
    ChColumn(
        "d128",
        "Decimal128(20)",
        "toDecimal128(n, 20) / 11",
        NUMBER,
        Target("numeric(38,20)"),
        Target("number(38,20)"),
    ),
    ChColumn(
        "d256",
        "Decimal256(40)",
        "toDecimal256(n, 40) / 13",
        NUMBER,
        Target("numeric(76,40)", out="toString(d256)"),
        Target("varchar2(80)", out="toString(d256)", src_ref="toString(d256)"),
    ),
    ChColumn(
        "s",
        "String",
        "concat('tab\\t', toString(n), '\\nnew\\\\line ''q'' \"dq\" ко 中文 🙂')",
        EXACT,
        Target("text"),
        Target("nvarchar2(200)"),
        unicode=True,
    ),
    ChColumn(
        "fs",
        "FixedString(4)",
        "toFixedString(concat('a', char(0), 'b'), 4)",
        BYTES,
        Target(
            "bytea",
            out="concat('\\\\x', hex(fs))",
            ref="encode(fs, 'hex')",
            src_ref="hex(fs)",
        ),
        Target("raw(4)", out="hex(fs)", src_ref="hex(fs)"),
    ),
    ChColumn(
        "lc",
        "LowCardinality(String)",
        "toLowCardinality(toString(n % 5))",
        EXACT,
        Target("text"),
        Target("varchar2(10)"),
    ),
    ChColumn(
        "dt",
        "Date",
        "toDate('1970-01-01') + n",
        DATETIME,
        Target("date", out="toDate32(dt)"),
        Target("date", out="toDate32(dt)"),
    ),
    ChColumn(
        "d32_",
        "Date32",
        "toDate32('1900-01-01') + n * 20",
        DATETIME,
        Target("date"),
        Target("date"),
    ),
    ChColumn(
        "dtm",
        "DateTime('UTC')",
        "toDateTime('2000-01-01 00:00:00', 'UTC') + n * 3601",
        DATETIME,
        Target(
            "timestamptz(0)",
            out="toDateTime64(dtm, 0, 'UTC')",
            ref="dtm at time zone 'UTC'",
        ),
        Target("date", out="toDateTime64(dtm, 0, 'UTC')"),
    ),
    ChColumn(
        "dt64",
        "DateTime64(6, 'UTC')",
        "toDateTime64('2000-01-01 00:00:00.123456', 6, 'UTC') + n * 3661.000001",
        DATETIME,
        Target("timestamptz(6)", ref="dt64 at time zone 'UTC'"),
        Target("timestamp(6)"),
    ),
    ChColumn(
        "dt9",
        "DateTime64(9, 'UTC')",
        "toDateTime64('2000-01-01 00:00:00.123456789', 9, 'UTC') + n",
        EXACT,
        Target("text", out="toString(dt9)", src_ref="toString(dt9)"),
        Target("varchar2(40)", out="toString(dt9)", src_ref="toString(dt9)"),
    ),
    ChColumn(
        "e8",
        "Enum8('x' = 1, 'y' = 2, 'z' = -3)",
        "CAST(['x', 'y', 'z'][n % 3 + 1], 'Enum8(''x'' = 1, ''y'' = 2, ''z'' = -3)')",
        EXACT,
        Target("text", out="toString(e8)", src_ref="toString(e8)"),
        Target("varchar2(4)", out="toString(e8)", src_ref="toString(e8)"),
        circle_major=23,
    ),
    ChColumn(
        "u",
        "UUID",
        "generateUUIDv4()",
        UUID,
        Target("uuid", out="toString(u)", src_ref="toString(u)"),
        Target("raw(16)", out="hex(u)", src_ref="hex(u)"),
        circle_major=26,
    ),
    ChColumn(
        "ip4",
        "IPv4",
        "toIPv4(concat('10.', toString(n % 256), '.1.2'))",
        EXACT,
        Target("inet", out="toString(ip4)", ref="host(ip4)", src_ref="toString(ip4)"),
        Target("varchar2(20)", out="toString(ip4)", src_ref="toString(ip4)"),
    ),
    ChColumn(
        "ip6",
        "IPv6",
        "toIPv6(concat('2001:db8::', hex(n)))",
        EXACT,
        Target("inet", out="toString(ip6)", ref="host(ip6)", src_ref="toString(ip6)"),
        Target("varchar2(50)", out="toString(ip6)", src_ref="toString(ip6)"),
        circle_major=23,
    ),
    ChColumn(
        "b",
        "Bool",
        "n % 2 = 0",
        NUMBER,
        Target("boolean"),
        Target("number(1)", out="toUInt8(b)"),
    ),
    ChColumn(
        "arr",
        "Array(Int64)",
        "[toInt64(n), -1, 0]",
        EXACT,
        Target("bigint[]", out="concat('{', arrayStringConcat(arr, ','), '}')"),
        Target("varchar2(100)", out="toString(arr)", src_ref="toString(arr)"),
    ),
    ChColumn(
        "aa",
        "Array(Array(String))",
        "[['a,b', 'c\"d'], [], ['{e}\\t']]",
        EXACT,
        Target("text", out="toString(aa)", src_ref="toString(aa)"),
        Target("varchar2(200)", out="toString(aa)", src_ref="toString(aa)"),
    ),
    ChColumn(
        "an",
        "Array(Nullable(Int32))",
        "[toNullable(toInt32(n)), null]",
        EXACT,
        Target("text", out="toString(an)", src_ref="toString(an)"),
        Target("varchar2(100)", out="toString(an)", src_ref="toString(an)"),
    ),
    ChColumn(
        "t",
        "Tuple(a Int32, b String, c Array(Float64))",
        "tuple(toInt32(n), 'x', [1.5, 2.25])",
        EXACT,
        Target("text", out="toString(t)", src_ref="toString(t)"),
        Target("varchar2(200)", out="toString(t)", src_ref="toString(t)"),
    ),
    ChColumn(
        "m",
        "Map(String, Int64)",
        "map('k', toInt64(n), 'z', 0)",
        EXACT,
        Target("text", out="toString(m)", src_ref="toString(m)"),
        Target("varchar2(200)", out="toString(m)", src_ref="toString(m)"),
    ),
    ChColumn(
        "pt",
        "Point",
        "(toFloat64(n), 0.5)",
        EXACT,
        Target("text", out="toString(pt)", src_ref="toString(pt)"),
        Target("varchar2(200)", out="toString(pt)", src_ref="toString(pt)"),
    ),
    ChColumn(
        "pg",
        "Polygon",
        "[[(0., 0.), (1., 0.), (1., 1.)], "
        "[(0.1, 0.1), (0.2, 0.1), (toFloat64(n), 0.2)]]",
        EXACT,
        Target("text", out="toString(pg)", src_ref="toString(pg)"),
        Target("varchar2(400)", out="toString(pg)", src_ref="toString(pg)"),
    ),
    ChColumn(
        "ns",
        "Nullable(String)",
        "if(n % 7 = 0, null, toString(n))",
        EXACT,
        Target("text"),
        Target("varchar2(20)"),
    ),
    ChColumn(
        "ni",
        "Nullable(Int64)",
        "if(n % 7 = 1, null, toInt64(n))",
        NUMBER,
        Target("bigint"),
        Target("number(19)"),
    ),
    ChColumn(
        "nd",
        "Nullable(Decimal(18, 4))",
        "if(n % 7 = 2, null, toDecimal64(n, 4) / 3)",
        NUMBER,
        Target("numeric(18,4)"),
        Target("number(18,4)"),
    ),
    ChColumn(
        "ndt",
        "Nullable(DateTime64(3, 'UTC'))",
        "if(n % 7 = 3, null, toDateTime64('2020-01-01', 3, 'UTC') + n)",
        DATETIME,
        Target("timestamptz(3)", ref="ndt at time zone 'UTC'"),
        Target("timestamp(3)"),
    ),
)


class Source:
    """Таблица src в базе ClickHouse с одной строкой на n = 1..ROWS."""

    def __init__(self, side: ClickHouseSide) -> None:
        self.side = side

    def columns(self) -> list[ChColumn]:
        return list(CH_COLUMNS)

    async def fill(self, rows: int) -> None:
        columns = self.columns()
        ddl = ", ".join(f"{c.name} {c.ch_type}" for c in columns)
        filled = ", ".join(f"({c.fill}) as {c.name}" for c in columns)
        await self.side.recreate_database()
        await self.side.command(
            f"create table {CH_DATABASE}.src ({ddl}) engine = MergeTree order by id"
        )
        await self.side.command(
            f"insert into {CH_DATABASE}.src select {filled} "
            f"from (select number as n from numbers(1, {rows}))"
        )


def _table_name(source_name: str) -> str:
    return "from_" + source_name.replace("-", "_").replace(".", "_")


def _names(columns: Sequence[ChColumn]) -> list[str]:
    return [c.name for c in columns]


@pytest.fixture(scope="module", params=STAND.demo_clickhouse(), ids=lambda s: s.name)
async def clickhouse(request: Any) -> AsyncIterator[Source]:
    side = ClickHouseSide(request.param, CH_DATABASE)
    await side.connect()
    source = Source(side)
    await source.fill(ROWS)
    yield source
    await side.drop()


@pytest.fixture(scope="module", params=STAND.sources, ids=lambda s: s.name)
async def postgres(request: Any) -> AsyncIterator[PostgresSide]:
    side = PostgresSide(request.param, PG_SCHEMA)
    await side.connect()
    await side.recreate_schema()
    yield side
    await side.drop()


@pytest.fixture(scope="module", params=STAND.ora_sources, ids=lambda s: s.name)
async def oracle(request: Any) -> AsyncIterator[OracleSide]:
    side = OracleSide(request.param, ARRAYSIZE)
    await side.connect()
    await side.recreate_user()
    yield side
    await side.drop()


class TestClickHouseToPostgres:
    async def test_every_clickhouse_type_lands(
        self, clickhouse: Source, postgres: PostgresSide
    ) -> None:
        columns: list[ChColumn] = []
        targets: list[Target] = []
        for column in clickhouse.columns():
            if column.pg is None:
                continue

            columns.append(column)
            targets.append(column.pg)

        table = _table_name(clickhouse.side.source.name)
        await postgres.create(
            table, [f"{c.name} {t.type}" for c, t in zip(columns, targets, strict=True)]
        )
        pumps = Pumps(postgres=postgres.profile, clickhouse=clickhouse.side.profile)
        chained = await pumps.chain(
            Leg(
                "ch_arrow_out",
                {
                    "sql": f"select {exported(_names(columns), targets, False)} "
                    f"from {CH_DATABASE}.src order by id settings {STRING_AS_STRING}",
                    "chunk_bytes": CHUNK_BYTES,
                },
            ),
            Leg(
                "pg_arrow_in",
                {
                    "sql": copy_into(f"{PG_SCHEMA}.{table}", [c.name for c in columns]),
                    "chunk_bytes": CHUNK_BYTES,
                },
            ),
        )
        assert chained.in_report.startswith(f"{ROWS} rows written")

        expected = await clickhouse.side.select(
            "src",
            [first(t.src_ref, c.name) for c, t in zip(columns, targets, strict=True)],
        )
        landed = await postgres.select(
            table, [first(t.ref, c.name) for c, t in zip(columns, targets, strict=True)]
        )
        report = compared(
            _names(columns),
            [c.compare for c in columns],
            [t.approx for t in targets],
            expected,
            landed,
        )

        assert not report.mismatches, report.render()


class TestClickHouseToOracle:
    async def test_every_clickhouse_type_lands(
        self, clickhouse: Source, oracle: OracleSide
    ) -> None:
        columns: list[ChColumn] = []
        targets: list[Target] = []
        for column in clickhouse.columns():
            if column.ora is None:
                continue

            if column.unicode and not oracle.unicode:
                continue

            columns.append(column)
            targets.append(column.ora)

        table = _table_name(clickhouse.side.source.name)
        await oracle.create(
            table, [f"{c.name} {t.type}" for c, t in zip(columns, targets, strict=True)]
        )
        pumps = Pumps(clickhouse=clickhouse.side.profile, oracle=oracle.profile)
        try:
            chained = await pumps.chain(
                Leg(
                    "ch_arrow_out",
                    {
                        "sql": f"select {exported(_names(columns), targets, False)} "
                        f"from {CH_DATABASE}.src order by id "
                        f"settings {STRING_AS_STRING}",
                        "chunk_bytes": CHUNK_BYTES,
                    },
                ),
                Leg(
                    "ora_arrow_in",
                    {
                        "sql": insert_into(table, [c.name for c in columns]),
                        "chunk_bytes": CHUNK_BYTES,
                    },
                ),
            )
            assert chained.in_report.startswith(f"{ROWS} rows written")

            expected = await clickhouse.side.select(
                "src",
                [
                    first(t.src_ref, c.name)
                    for c, t in zip(columns, targets, strict=True)
                ],
            )
            landed = await oracle.select(
                table,
                [first(t.ref, c.name) for c, t in zip(columns, targets, strict=True)],
            )
        finally:
            await oracle.drop_table(table)

        report = compared(
            _names(columns),
            [c.compare for c in columns],
            [t.approx for t in targets],
            expected,
            landed,
        )

        assert not report.mismatches, report.render()


class TestClickHouseToClickHouse:
    async def test_native_types_survive_the_circle(self, clickhouse: Source) -> None:
        columns: list[ChColumn] = []
        for column in clickhouse.columns():
            if clickhouse.side.major < column.circle_major:
                continue

            columns.append(column)

        names = ", ".join(_names(columns))
        await clickhouse.side.create(
            "circle", [f"{c.name} {c.ch_type}" for c in columns]
        )
        pumps = Pumps(clickhouse=clickhouse.side.profile)
        chained = await pumps.chain(
            Leg(
                "ch_arrow_out",
                {
                    "sql": f"select {names} from {CH_DATABASE}.src order by id "
                    f"settings {STRING_AS_STRING}",
                    "chunk_bytes": CHUNK_BYTES,
                },
            ),
            Leg(
                "ch_arrow_in",
                {
                    "sql": f"insert into {CH_DATABASE}.circle format ArrowStream",
                    "chunk_bytes": CHUNK_BYTES,
                },
            ),
        )
        assert chained.in_report.startswith(f"{ROWS} rows written")

        refs = [f"toString({c.name})" for c in columns]
        expected = await clickhouse.side.select("src", refs)
        landed = await clickhouse.side.select("circle", refs)
        report = Report()
        ids = [row[0] for row in expected]
        for position, column in enumerate(columns):
            report.compare(
                column.name,
                EXACT,
                0.0,
                ids,
                [row[position] for row in expected],
                [row[position] for row in landed],
            )

        assert not report.mismatches, report.render()
