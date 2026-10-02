# ruff: noqa: S608, E501
"""Перекачка из ClickHouse потоком Arrow IPC: ch_stream_out против pg_stream_in
(каждый ClickHouse из ch_sources в каждый postgres и Greenplum из sources) и
ora_stream_in (в каждый Oracle из ora_sources). Насосы соединены трубой ОС и
работают одновременно; круг ClickHouse -> ClickHouse проверяет test_ch_sync.

Каждый запуск описан toml-текстом (DagSpec) и исполняется DagRunner без
модели; список select под приёмник и имя таблицы собираются из матрицы
колонок и подставляются в описание именами $pg_sql/$pg_table и
$ora_sql/$ora_table.

Таблица ClickHouse несёт все семейства типов: целые до 256 бит, Decimal до
256 бит, Float с NaN и бесконечностями, String и FixedString с NUL,
LowCardinality, Date и Date32, DateTime и DateTime64 до наносекунд, Enum,
UUID, IPv4/IPv6, Bool, массивы, Tuple, Map, гео-типы и Nullable. У каждой
колонки записано, что писать в select под конкретный приёмник: родной тип
Arrow там, где приёмник его читает, и явный toString или hex там, где нет
(широкие целые, DateTime64(9), Enum, UUID, IP, составные), — так по самим
тестам видно, какие типы едут только строкой."""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from boba.pump_stand import (
    ClickHouseSide,
    OracleSide,
    PostgresSide,
    PumpDags,
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
    Values,
)
from boba.pump_stand.matrix import (
    Target,
    compared,
    exported,
    first,
)
from boba.pump_stand.oracle import PumpUser
from boba.stand.names import StandNames

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

STAND = PumpStand.required()
ROWS = 60
ARRAYSIZE = 97
PG_SCHEMA = StandNames().of("pump_ch_arrow")
CH_DATABASE = StandNames().of("pump_ch_arrow")

TO_POSTGRES = """
name = "ch_to_pg_arrow_every_type"
version = 1

[[nodes]]
key = "src"
tool = "ch_stream_out"

[nodes.args]
connection = "ch"
sql = '''select $pg_sql from $ch_database.src order by id settings output_format_arrow_string_as_string = 1'''
wire = "arrow"
chunk_bytes = 4096
out = "rows"

[[nodes]]
key = "pg"
tool = "pg_stream_in"

[nodes.args]
connection = "pg"
schema_name = "$pg_schema"
table_name = "$pg_table"
schema_strategy = { kind = "create_if_not_exists" }
delete_strategy = { kind = "nothing" }
insert_strategy = { kind = "full" }
copy_options = { chunk_bytes = 4096 }
feed = "rows"
"""

TO_ORACLE = """
name = "ch_to_ora_arrow_every_type"
version = 1

[[nodes]]
key = "src"
tool = "ch_stream_out"

[nodes.args]
connection = "ch"
sql = '''select $ora_sql from $ch_database.src order by id settings output_format_arrow_string_as_string = 1'''
wire = "arrow"
chunk_bytes = 4096
out = "rows"

[[nodes]]
key = "ora"
tool = "ora_stream_in"

[nodes.args]
connection = "ora"
schema_name = "$ora_user"
table_name = "$ora_table"
schema_strategy = { kind = "error_if_not_exists" }
delete_strategy = { kind = "nothing" }
insert_strategy = { kind = "full" }
chunk_bytes = 4096
feed = "rows"
"""


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
        Target("varchar2(8)", out="hex(fs)", src_ref="hex(fs)"),
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
        Target("varchar2(32)", out="hex(u)", src_ref="hex(u)"),
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


@dataclass(frozen=True)
class Landing:
    """Колонки матрицы, которые берёт приёмник, и их стороны у него."""

    columns: Sequence[ChColumn]
    targets: Sequence[Target]

    def names(self) -> list[str]:
        return [c.name for c in self.columns]

    def ddl(self) -> list[str]:
        return [
            f"{c.name} {t.type}"
            for c, t in zip(self.columns, self.targets, strict=True)
        ]

    def exported(self) -> str:
        return exported(self.names(), self.targets, False)

    def source_refs(self) -> list[str]:
        return [
            first(t.src_ref, c.name)
            for c, t in zip(self.columns, self.targets, strict=True)
        ]

    def target_refs(self) -> list[str]:
        return [
            first(t.ref, c.name)
            for c, t in zip(self.columns, self.targets, strict=True)
        ]

    def compares(self) -> list[Values]:
        return [c.compare for c in self.columns]

    def tolerances(self) -> list[float]:
        return [t.approx for t in self.targets]


class Source:
    """Таблица src в базе ClickHouse с одной строкой на n = 1..ROWS."""

    def __init__(self, side: ClickHouseSide) -> None:
        self.side = side

    def columns(self) -> list[ChColumn]:
        return list(CH_COLUMNS)

    def for_postgres(self) -> Landing:
        columns: list[ChColumn] = []
        targets: list[Target] = []
        for column in self.columns():
            if column.pg is None:
                continue

            columns.append(column)
            targets.append(column.pg)

        return Landing(columns, targets)

    def for_oracle(self, unicode: bool) -> Landing:
        columns: list[ChColumn] = []
        targets: list[Target] = []
        for column in self.columns():
            if column.ora is None:
                continue

            if column.unicode and not unicode:
                continue

            columns.append(column)
            targets.append(column.ora)

        return Landing(columns, targets)

    def table_name(self) -> str:
        """Имя таблицы приёмника по имени источника ClickHouse."""
        return "from_" + self.side.source.name.replace("-", "_").replace(".", "_")

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


@pytest.fixture
def pg_dags(tmp_path: Path, clickhouse: Source, postgres: PostgresSide) -> PumpDags:
    return PumpDags(
        tmp_path,
        {"ch": clickhouse.side.profile, "pg": postgres.profile},
        {
            "ch_database": CH_DATABASE,
            "pg_schema": PG_SCHEMA,
            "pg_table": clickhouse.table_name(),
            "pg_sql": clickhouse.for_postgres().exported(),
        },
    )


@pytest.fixture
def ora_dags(tmp_path: Path, clickhouse: Source, oracle: OracleSide) -> PumpDags:
    return PumpDags(
        tmp_path,
        {"ch": clickhouse.side.profile, "ora": oracle.profile},
        {
            "ch_database": CH_DATABASE,
            "ora_user": PumpUser().name,
            "ora_table": clickhouse.table_name(),
            "ora_sql": clickhouse.for_oracle(oracle.unicode).exported(),
        },
    )


class TestClickHouseToPostgres:
    async def test_every_clickhouse_type_lands(
        self, pg_dags: PumpDags, clickhouse: Source, postgres: PostgresSide
    ) -> None:
        landing = clickhouse.for_postgres()
        table = clickhouse.table_name()
        await postgres.create(table, landing.ddl())

        outcome = await pg_dags.run(TO_POSTGRES)
        report = outcome.node("pg").content

        assert outcome.ok(), report
        assert report.startswith(f"{ROWS} rows loaded")

        expected = await clickhouse.side.select("src", landing.source_refs())
        landed = await postgres.select(table, landing.target_refs())
        report = compared(
            landing.names(),
            landing.compares(),
            landing.tolerances(),
            expected,
            landed,
        )

        assert not report.mismatches, report.render()


class TestClickHouseToOracle:
    async def test_every_clickhouse_type_lands(
        self, ora_dags: PumpDags, clickhouse: Source, oracle: OracleSide
    ) -> None:
        landing = clickhouse.for_oracle(oracle.unicode)
        table = clickhouse.table_name()
        await oracle.create(table, landing.ddl())
        try:
            outcome = await ora_dags.run(TO_ORACLE)
            report = outcome.node("ora").content

            assert outcome.ok(), report
            assert report.startswith(f"{ROWS} rows loaded")

            expected = await clickhouse.side.select("src", landing.source_refs())
            landed = await oracle.select(table, landing.target_refs())
        finally:
            await oracle.drop_table(table)

        report = compared(
            landing.names(),
            landing.compares(),
            landing.tolerances(),
            expected,
            landed,
        )

        assert not report.mismatches, report.render()
