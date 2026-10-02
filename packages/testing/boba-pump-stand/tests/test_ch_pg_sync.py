# ruff: noqa: S608, E501
"""Пара ClickHouse -> postgres по tsv: ch_stream_out с wire = tsv в pg_stream_in на
приёмниках pg-16 и Greenplum 7, источник — каждый ClickHouse стенда с
таблицей всех ходовых типов.

Каждый запуск описан toml-текстом (DagSpec) и исполняется DagRunner без
модели: узлы — вызовы насосов с аргументами, как их присылает модель.

Что проверяется:
    - каждый тип ClickHouse ложится своим типом postgres, значения совпадают
      текстом: целые до 256 бит, Decimal, Float с nan и inf, строки,
      LowCardinality и Enum, даты и DateTime64 в UTC, Bool, UUID, IPv4 и
      IPv6, JSON;
    - типы без пары (Array, Map, Tuple, DateTime64 не в UTC): отказ с
      подсказкой или text по fallback_as_varchar, объявленный тип по
      column_types;
    - сверка с существующей таблицей postgres: шире — предупреждение, уже —
      отказ до загрузки;
    - ловушка: FixedString с NUL-байтами postgres не примет, транзакция
      откатывается;
    - повторная загрузка, витрина через rename и column_types.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator, Sequence
from decimal import Decimal
from pathlib import Path
from typing import Any, ClassVar

import pytest

from boba.pump_stand import (
    ChLoaded,
    ClickHouseSide,
    Loaded,
    PostgresSide,
    PumpDags,
    PumpStand,
)
from boba.stand.names import StandNames

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

STAND = PumpStand.required()
PG_SCHEMA = StandNames().of("pump_ch_pg")
CH_DATABASE = StandNames().of("pump_ch_pg")
ROWS = 60
TARGET_NAMES = ("pg-16", "gp-7")
JSON_SINCE = 24
JSON_SETTINGS = {
    "allow_experimental_object_type": 1,
    "allow_experimental_json_type": 1,
}

TYPED_DDL = f"""
create table {CH_DATABASE}.src (
    id Int64,
    i8 Int8,
    u16 UInt16,
    u64 UInt64,
    big Int128,
    huge UInt256,
    f32 Float32,
    f64 Float64,
    dec Decimal(18, 4),
    wide Decimal(60, 10),
    s String,
    ns Nullable(String),
    lc LowCardinality(String),
    en Enum8('sad' = 1, 'happy' = 2),
    d Date,
    d32 Date32,
    dt DateTime('UTC'),
    dt64 DateTime64(6, 'UTC'),
    naive DateTime64(3),
    b Bool,
    u UUID,
    ip4 IPv4,
    ip6 IPv6
) engine = MergeTree order by id"""

TYPED_FILL = f"""
insert into {CH_DATABASE}.src
select number, toInt8(number % 100 - 50), toUInt16(number % 60000),
       toUInt64(number) * 1000000007, toInt128(number) * -1000000000000,
       toUInt256(number) * toUInt256('1000000000000000000000'),
       if(number % 7 = 0, nan, number / 3), if(number % 11 = 0, inf, number / 7),
       toDecimal64(number, 4) / 7, toDecimal256(number, 10) / 3,
       concat('s ', toString(number), ' \\t tab \\\\ back'),
       if(number % 5 = 0, null, concat('n', toString(number))),
       concat('lc', toString(number % 3)), if(number % 2 = 0, 'sad', 'happy'),
       toDate('2024-01-01') + number % 300, toDate32('1950-01-01') + number % 300,
       toDateTime('2024-02-29 13:14:15', 'UTC') + number,
       toDateTime64('2024-02-29 13:14:15.123456', 6, 'UTC') + number,
       toDateTime64('2024-02-29 13:14:15.123', 3) + number,
       number % 2 = 0, generateUUIDv4(),
       toIPv4('10.1.2.3'), toIPv6('2001:db8::1')
from numbers({ROWS})"""

EXPECTED_COLUMNS = [
    ("id", "bigint", True),
    ("i8", "smallint", True),
    ("u16", "integer", True),
    ("u64", "numeric(20,0)", True),
    ("big", "numeric(39,0)", True),
    ("huge", "numeric(78,0)", True),
    ("f32", "real", True),
    ("f64", "double precision", True),
    ("dec", "numeric(18,4)", True),
    ("wide", "numeric(60,10)", True),
    ("s", "text", True),
    ("ns", "text", False),
    ("lc", "text", True),
    ("en", "text", True),
    ("d", "date", True),
    ("d32", "date", True),
    ("dt", "timestamp(0) with time zone", True),
    ("dt64", "timestamp(6) with time zone", True),
    ("naive", "timestamp(3) without time zone", True),
    ("b", "boolean", True),
    ("u", "uuid", True),
    ("ip4", "inet", True),
    ("ip6", "inet", True),
]
"""Колонки postgres по контракту ClickHouse: имя, тип, not null."""

TYPES_CREATE = """
name = "ch_to_pg_tsv_types_create"
version = 1

[[nodes]]
key = "src"
tool = "ch_stream_out"

[nodes.args]
connection = "ch"
sql = "select * from $ch_database.src order by id"
wire = "tsv"
chunk_bytes = 65536
out = "rows"

[[nodes]]
key = "pg"
tool = "pg_stream_in"

[nodes.args]
connection = "pg"
schema_name = "$pg_schema"
table_name = "types"
schema_strategy = { kind = "drop_and_create" }
delete_strategy = { kind = "nothing" }
insert_strategy = { kind = "full" }
rules = {}
unknown_types = { kind = "fail_on_unknown" }
copy_options = { chunk_bytes = 65536 }
feed = "rows"
"""

TYPES_RELOAD = """
name = "ch_to_pg_tsv_types_reload"
version = 1

[[nodes]]
key = "src"
tool = "ch_stream_out"

[nodes.args]
connection = "ch"
sql = "select * from $ch_database.src order by id"
wire = "tsv"
chunk_bytes = 65536
out = "rows"

[[nodes]]
key = "pg"
tool = "pg_stream_in"

[nodes.args]
connection = "pg"
schema_name = "$pg_schema"
table_name = "types"
schema_strategy = { kind = "error_if_schema_changed" }
delete_strategy = { kind = "truncate" }
insert_strategy = { kind = "full" }
rules = {}
unknown_types = { kind = "fail_on_unknown" }
copy_options = { chunk_bytes = 65536 }
feed = "rows"
"""

JSON_TABLE = """
name = "ch_to_pg_tsv_json"
version = 1

[[nodes]]
key = "src"
tool = "ch_stream_out"

[nodes.args]
connection = "ch"
sql = "select * from $ch_database.js order by id"
wire = "tsv"
chunk_bytes = 65536
out = "rows"

[[nodes]]
key = "pg"
tool = "pg_stream_in"

[nodes.args]
connection = "pg"
schema_name = "$pg_schema"
table_name = "js"
schema_strategy = { kind = "drop_and_create" }
delete_strategy = { kind = "nothing" }
insert_strategy = { kind = "full" }
rules = {}
unknown_types = { kind = "fail_on_unknown" }
copy_options = { chunk_bytes = 65536 }
feed = "rows"
"""

UNKNOWN_ARRAY = """
name = "ch_to_pg_tsv_unknown_array"
version = 1

[[nodes]]
key = "src"
tool = "ch_stream_out"

[nodes.args]
connection = "ch"
sql = "select id, [id, id + 1] as arr from $ch_database.src order by id"
wire = "tsv"
chunk_bytes = 65536
out = "rows"

[[nodes]]
key = "pg"
tool = "pg_stream_in"

[nodes.args]
connection = "pg"
schema_name = "$pg_schema"
table_name = "unknown"
schema_strategy = { kind = "drop_and_create" }
delete_strategy = { kind = "nothing" }
insert_strategy = { kind = "full" }
rules = {}
unknown_types = { kind = "fail_on_unknown" }
copy_options = { chunk_bytes = 65536 }
feed = "rows"
"""

UNKNOWN_MAP = """
name = "ch_to_pg_tsv_unknown_map"
version = 1

[[nodes]]
key = "src"
tool = "ch_stream_out"

[nodes.args]
connection = "ch"
sql = "select id, map('k', id) as m from $ch_database.src order by id"
wire = "tsv"
chunk_bytes = 65536
out = "rows"

[[nodes]]
key = "pg"
tool = "pg_stream_in"

[nodes.args]
connection = "pg"
schema_name = "$pg_schema"
table_name = "unknown"
schema_strategy = { kind = "drop_and_create" }
delete_strategy = { kind = "nothing" }
insert_strategy = { kind = "full" }
rules = {}
unknown_types = { kind = "fail_on_unknown" }
copy_options = { chunk_bytes = 65536 }
feed = "rows"
"""

UNKNOWN_TUPLE = """
name = "ch_to_pg_tsv_unknown_tuple"
version = 1

[[nodes]]
key = "src"
tool = "ch_stream_out"

[nodes.args]
connection = "ch"
sql = "select id, (id, 'x') as t from $ch_database.src order by id"
wire = "tsv"
chunk_bytes = 65536
out = "rows"

[[nodes]]
key = "pg"
tool = "pg_stream_in"

[nodes.args]
connection = "pg"
schema_name = "$pg_schema"
table_name = "unknown"
schema_strategy = { kind = "drop_and_create" }
delete_strategy = { kind = "nothing" }
insert_strategy = { kind = "full" }
rules = {}
unknown_types = { kind = "fail_on_unknown" }
copy_options = { chunk_bytes = 65536 }
feed = "rows"
"""

UNKNOWN_LOCAL_TIME = """
name = "ch_to_pg_tsv_unknown_local_time"
version = 1

[[nodes]]
key = "src"
tool = "ch_stream_out"

[nodes.args]
connection = "ch"
sql = "select id, toDateTime64(dt64, 3, 'Europe/Moscow') as local from $ch_database.src order by id"
wire = "tsv"
chunk_bytes = 65536
out = "rows"

[[nodes]]
key = "pg"
tool = "pg_stream_in"

[nodes.args]
connection = "pg"
schema_name = "$pg_schema"
table_name = "unknown"
schema_strategy = { kind = "drop_and_create" }
delete_strategy = { kind = "nothing" }
insert_strategy = { kind = "full" }
rules = {}
unknown_types = { kind = "fail_on_unknown" }
copy_options = { chunk_bytes = 65536 }
feed = "rows"
"""

UNKNOWN_AS_TEXT = """
name = "ch_to_pg_tsv_unknown_as_text"
version = 1

[[nodes]]
key = "src"
tool = "ch_stream_out"

[nodes.args]
connection = "ch"
sql = "select id, [id, id + 1] as arr, map('k', id) as m, (id, 'x') as t, toDateTime64(dt64, 3, 'Europe/Moscow') as local from $ch_database.src order by id"
wire = "tsv"
chunk_bytes = 65536
out = "rows"

[[nodes]]
key = "pg"
tool = "pg_stream_in"

[nodes.args]
connection = "pg"
schema_name = "$pg_schema"
table_name = "unknown"
schema_strategy = { kind = "drop_and_create" }
delete_strategy = { kind = "nothing" }
insert_strategy = { kind = "full" }
rules = {}
unknown_types = { kind = "fallback_as_varchar" }
copy_options = { chunk_bytes = 65536 }
feed = "rows"
"""

DECLARED_TYPES = """
name = "ch_to_pg_tsv_declared_types"
version = 1

[[nodes]]
key = "src"
tool = "ch_stream_out"

[nodes.args]
connection = "ch"
sql = "select id, u64, toDateTime64(dt64, 3, 'Europe/Moscow') as local from $ch_database.src order by id"
wire = "tsv"
chunk_bytes = 65536
out = "rows"

[[nodes]]
key = "pg"
tool = "pg_stream_in"

[nodes.args]
connection = "pg"
schema_name = "$pg_schema"
table_name = "declared"
schema_strategy = { kind = "drop_and_create" }
delete_strategy = { kind = "nothing" }
insert_strategy = { kind = "full" }
rules = { column_types = { local = "timestamp(3)", u64 = "numeric(30, 2)" } }
unknown_types = { kind = "fail_on_unknown" }
copy_options = { chunk_bytes = 65536 }
feed = "rows"
"""

EXISTING_WIDER = """
name = "ch_to_pg_tsv_existing_wider"
version = 1

[[nodes]]
key = "src"
tool = "ch_stream_out"

[nodes.args]
connection = "ch"
sql = "select id, i8, dec, s, dt64 from $ch_database.src order by id"
wire = "tsv"
chunk_bytes = 65536
out = "rows"

[[nodes]]
key = "pg"
tool = "pg_stream_in"

[nodes.args]
connection = "pg"
schema_name = "$pg_schema"
table_name = "existing"
schema_strategy = { kind = "error_if_not_exists" }
delete_strategy = { kind = "nothing" }
insert_strategy = { kind = "full" }
rules = {}
unknown_types = { kind = "fail_on_unknown" }
copy_options = { chunk_bytes = 65536 }
feed = "rows"
"""

EXISTING_CHECK = """
name = "ch_to_pg_tsv_existing_check"
version = 1

[[nodes]]
key = "src"
tool = "ch_stream_out"

[nodes.args]
connection = "ch"
sql = "select id, i8, dec, s, dt64 from $ch_database.src order by id"
wire = "tsv"
chunk_bytes = 65536
out = "rows"

[[nodes]]
key = "pg"
tool = "pg_stream_in"

[nodes.args]
connection = "pg"
schema_name = "$pg_schema"
table_name = "existing"
schema_strategy = { kind = "error_if_schema_changed" }
delete_strategy = { kind = "nothing" }
insert_strategy = { kind = "full" }
rules = {}
unknown_types = { kind = "fail_on_unknown" }
copy_options = { chunk_bytes = 65536 }
feed = "rows"
"""

FIXED_WITH_NUL = """
name = "ch_to_pg_tsv_fixed_string_with_nul"
version = 1

[[nodes]]
key = "src"
tool = "ch_stream_out"

[nodes.args]
connection = "ch"
sql = "select id, toFixedString('ab', 4) as fs from $ch_database.src"
wire = "tsv"
chunk_bytes = 65536
out = "rows"

[[nodes]]
key = "pg"
tool = "pg_stream_in"

[nodes.args]
connection = "pg"
schema_name = "$pg_schema"
table_name = "fixed"
schema_strategy = { kind = "create_if_not_exists" }
delete_strategy = { kind = "nothing" }
insert_strategy = { kind = "full" }
rules = {}
unknown_types = { kind = "fail_on_unknown" }
copy_options = { chunk_bytes = 65536 }
feed = "rows"
"""

FIXED_TRIMMED = """
name = "ch_to_pg_tsv_fixed_string_trimmed"
version = 1

[[nodes]]
key = "src"
tool = "ch_stream_out"

[nodes.args]
connection = "ch"
sql = '''select id, replaceAll(toString(toFixedString('ab', 4)), '\\\\0', '') as fs from $ch_database.src order by id'''
wire = "tsv"
chunk_bytes = 65536
out = "rows"

[[nodes]]
key = "pg"
tool = "pg_stream_in"

[nodes.args]
connection = "pg"
schema_name = "$pg_schema"
table_name = "fixed"
schema_strategy = { kind = "drop_and_create" }
delete_strategy = { kind = "nothing" }
insert_strategy = { kind = "full" }
rules = {}
unknown_types = { kind = "fail_on_unknown" }
copy_options = { chunk_bytes = 65536 }
feed = "rows"
"""

MART = """
name = "ch_to_pg_tsv_mart"
version = 1

[[nodes]]
key = "src"
tool = "ch_stream_out"

[nodes.args]
connection = "ch"
sql = "select id, dec, lc from $ch_database.src order by id"
wire = "tsv"
chunk_bytes = 65536
out = "rows"

[[nodes]]
key = "pg"
tool = "pg_stream_in"

[nodes.args]
connection = "pg"
schema_name = "$pg_schema"
table_name = "mart"
schema_strategy = { kind = "drop_and_create" }
delete_strategy = { kind = "nothing" }
insert_strategy = { kind = "full" }
rules = { rename_columns = { key = "id", amount = "dec", label = "lc" }, column_types = { amount = "numeric(20, 2)", label = "varchar(10)" } }
unknown_types = { kind = "fail_on_unknown" }
copy_options = { chunk_bytes = 65536 }
feed = "rows"
"""


def _sources_by_name() -> dict[str, Any]:
    return {source.name: source for source in STAND.sources}


def _targets() -> list[Any]:
    by_name = _sources_by_name()
    chosen: list[Any] = []
    for name in TARGET_NAMES:
        if name in by_name:
            chosen.append(by_name[name])

    return chosen


@pytest.fixture(scope="module", params=STAND.demo_clickhouse(), ids=lambda s: s.name)
async def clickhouse(request: Any) -> AsyncIterator[ClickHouseSide]:
    side = ClickHouseSide(request.param, CH_DATABASE)
    await side.connect()
    await side.recreate_database()
    await side.command(TYPED_DDL)
    await side.command(TYPED_FILL)

    yield side

    await side.drop()


@pytest.fixture(scope="module", params=_targets(), ids=lambda s: s.name)
async def postgres(request: Any) -> AsyncIterator[PostgresSide]:
    side = PostgresSide(request.param, PG_SCHEMA)
    await side.connect()
    await side.recreate_schema()

    yield side

    await side.drop()


@pytest.fixture
def dags(
    tmp_path: Path, clickhouse: ClickHouseSide, postgres: PostgresSide
) -> PumpDags:
    return PumpDags(
        tmp_path,
        {"ch": clickhouse.profile, "pg": postgres.profile},
        {"ch_database": CH_DATABASE, "pg_schema": PG_SCHEMA},
    )


async def _landed(
    dags: PumpDags, clickhouse: ClickHouseSide, postgres: PostgresSide, spec: str
) -> str:
    """Запуск описания; отчёт приёмника — узла pg."""
    outcome = await dags.run(spec)
    report = outcome.node("pg").content
    print(
        f"\n--- {clickhouse.source.name} -> {postgres.source.name}: {outcome.dag} "
        f"---\n{report}"
    )

    assert outcome.ok(), report

    return report


class TestNativeTypes:
    async def test_types_and_values_land(
        self, dags: PumpDags, clickhouse: ClickHouseSide, postgres: PostgresSide
    ) -> None:
        report = await _landed(dags, clickhouse, postgres, TYPES_CREATE)
        landed = Loaded(postgres, PG_SCHEMA, "types")
        source = ChLoaded(clickhouse, "src")

        assert f"{ROWS} rows loaded" in report
        assert await landed.columns() == EXPECTED_COLUMNS
        assert await landed.count() == ROWS
        assert await landed.aggregate("sum(u64)") == Decimal(
            str(await source.scalar("sum(toInt256(u64))"))
        )
        assert await landed.aggregate("sum(big)") == Decimal(
            str(await source.scalar("sum(toInt256(big))"))
        )
        assert await landed.aggregate("sum(dec)") == Decimal(
            str(await source.scalar("sum(dec)"))
        )
        assert await landed.aggregate("sum(wide)") == Decimal(
            str(await source.scalar("sum(wide)"))
        )
        assert await landed.aggregate("count(*) filter (where f32 = 'NaN')") == (
            await source.scalar("countIf(isNaN(f32))")
        )
        assert await landed.aggregate("count(*) filter (where f64 = 'Infinity')") == (
            await source.scalar("countIf(isInfinite(f64))")
        )
        assert await landed.aggregate("count(*) filter (where ns is null)") == (
            await source.scalar("countIf(ns is null)")
        )
        assert await landed.aggregate("count(*) filter (where b)") == (
            await source.scalar("countIf(b)")
        )
        assert await landed.texts("s") == await source.column("s")
        assert await landed.texts("huge") == await source.column("toString(huge)")
        assert await landed.texts("lc") == await source.column("lc")
        assert await landed.texts("en") == await source.column("toString(en)")
        assert await landed.texts("d") == await source.column("toString(d)")
        assert await landed.texts("d32") == await source.column("toString(d32)")
        assert await landed.scalars(
            "to_char(dt64 at time zone 'UTC', 'YYYY-MM-DD HH24:MI:SS.US')"
        ) == await source.column("toString(dt64)")
        assert await landed.scalars(
            "to_char(naive, 'YYYY-MM-DD HH24:MI:SS.MS')"
        ) == await source.column("toString(naive)")
        assert await landed.texts("u") == await source.column("toString(u)")
        assert await landed.scalars("host(ip4)") == await source.column("toString(ip4)")
        assert await landed.scalars("host(ip6)") == await source.column("toString(ip6)")

    async def test_reload_passes_the_check(
        self, dags: PumpDags, clickhouse: ClickHouseSide, postgres: PostgresSide
    ) -> None:
        report = await _landed(dags, clickhouse, postgres, TYPES_RELOAD)

        assert f"{ROWS} rows loaded" in report
        assert await Loaded(postgres, PG_SCHEMA, "types").count() == ROWS

    async def test_json_lands_as_jsonb(
        self, dags: PumpDags, clickhouse: ClickHouseSide, postgres: PostgresSide
    ) -> None:
        if clickhouse.major < JSON_SINCE:
            pytest.skip("JSON of this server is Object('json') and prints a tuple")

        await clickhouse.command(f"drop table if exists {CH_DATABASE}.js")
        await clickhouse.command(
            f"create table {CH_DATABASE}.js (id Int64, j JSON) "
            "engine = MergeTree order by id",
            settings=JSON_SETTINGS,
        )
        await clickhouse.command(
            f"insert into {CH_DATABASE}.js select number, "
            'concat(\'{"g": \', toString(number), \', "tag": "x"}\') '
            f"from numbers({ROWS})",
            settings=JSON_SETTINGS,
        )

        report = await _landed(dags, clickhouse, postgres, JSON_TABLE)
        landed = Loaded(postgres, PG_SCHEMA, "js")

        assert f"{ROWS} rows loaded" in report
        assert await landed.columns() == [("id", "bigint", True), ("j", "jsonb", True)]
        assert await landed.aggregate("sum((j ->> 'g')::bigint)") == (
            ROWS * (ROWS - 1) // 2
        )


class TestUnknownTypes:
    """Array, Map, Tuple и DateTime64 не в UTC: у postgres нет типа, который
    прочитает текст ClickHouse без потерь."""

    REFUSED: ClassVar[Sequence[tuple[str, str, str]]] = (
        ("arr", "Array\\(Int64\\)", UNKNOWN_ARRAY),
        ("m", "Map\\(String, Int64\\)", UNKNOWN_MAP),
        ("t", "Tuple\\(", UNKNOWN_TUPLE),
        ("local", "TabSeparated carries no offset", UNKNOWN_LOCAL_TIME),
    )

    @pytest.mark.parametrize(("column", "message", "spec"), REFUSED)
    async def test_refused_by_default(
        self, dags: PumpDags, column: str, message: str, spec: str
    ) -> None:
        outcome = await dags.run(spec)

        failure = outcome.failure("pg")
        assert failure.error_kind == "TransferError", failure
        assert re.search(f"column {column}: .*{message}", failure.llm_view()), (
            failure.llm_view()
        )

    async def test_land_as_text_on_fallback(
        self, dags: PumpDags, clickhouse: ClickHouseSide, postgres: PostgresSide
    ) -> None:
        report = await _landed(dags, clickhouse, postgres, UNKNOWN_AS_TEXT)
        landed = Loaded(postgres, PG_SCHEMA, "unknown")

        assert f"{ROWS} rows loaded" in report
        assert await landed.columns() == [
            ("id", "bigint", True),
            ("arr", "character varying", True),
            ("m", "character varying", True),
            ("t", "character varying", True),
            ("local", "character varying", True),
        ]
        assert (await landed.texts("arr"))[:2] == ["[0,1]", "[1,2]"]
        assert (await landed.texts("m"))[:1] == ["{'k':0}"]

    async def test_declared_type_wins(
        self, dags: PumpDags, clickhouse: ClickHouseSide, postgres: PostgresSide
    ) -> None:
        report = await _landed(dags, clickhouse, postgres, DECLARED_TYPES)
        landed = Loaded(postgres, PG_SCHEMA, "declared")

        assert f"{ROWS} rows loaded" in report
        assert await landed.columns() == [
            ("id", "bigint", True),
            ("u64", "numeric(30,2)", True),
            ("local", "timestamp(3) without time zone", True),
        ]
        assert await landed.scalars(
            "to_char(local, 'YYYY-MM-DD HH24:MI:SS.MS')"
        ) == await ChLoaded(clickhouse, "src").column(
            "toString(toDateTime64(dt64, 3, 'Europe/Moscow'))"
        )


class TestExistingTable:
    async def create(self, postgres: PostgresSide, columns: str) -> None:
        await postgres.execute(
            [
                f"drop table if exists {PG_SCHEMA}.existing",
                f"create table {PG_SCHEMA}.existing ({columns})",
            ]
        )

    async def test_wider_table_takes_the_stream(
        self, dags: PumpDags, clickhouse: ClickHouseSide, postgres: PostgresSide
    ) -> None:
        await self.create(
            postgres,
            "id numeric(20) not null, i8 integer, dec numeric(30, 6), "
            "s varchar(200), dt64 timestamptz",
        )

        report = await _landed(dags, clickhouse, postgres, EXISTING_WIDER)

        assert f"{ROWS} rows loaded" in report
        assert re.search(
            r'"column": "dec",[^}]*"note": "table numeric\(30,6\) is wider', report
        )
        assert await Loaded(postgres, PG_SCHEMA, "existing").count() == ROWS

    CASES: ClassVar[Sequence[tuple[str, str]]] = (
        (
            "id bigint not null, i8 smallint, dec numeric(18, 2), s text, "
            "dt64 timestamptz(6)",
            "dec: table numeric\\(18,2\\) truncates the scale",
        ),
        (
            "id bigint not null, i8 smallint, dec numeric(18, 4), s text, "
            "dt64 timestamptz(3)",
            "dt64: table timestamp\\(3\\) with time zone is coarser",
        ),
        (
            "id bigint not null, i8 integer, dec numeric(18, 4), s text, "
            "dt64 timestamptz(6)",
            "i8: type differs",
        ),
        (
            "id bigint not null, i8 smallint, dec numeric(18, 4), s text not null, "
            "dt64 timestamptz(6)",
            "ok",
        ),
    )

    @pytest.mark.parametrize(("columns", "message"), CASES)
    async def test_schema_check(
        self,
        dags: PumpDags,
        clickhouse: ClickHouseSide,
        postgres: PostgresSide,
        columns: str,
        message: str,
    ) -> None:
        await self.create(postgres, columns)
        if message == "ok":
            report = await _landed(dags, clickhouse, postgres, EXISTING_CHECK)

            assert f"{ROWS} rows loaded" in report

            return

        outcome = await dags.run(EXISTING_CHECK)

        failure = outcome.failure("pg")
        assert failure.error_kind == "TransferError", failure
        assert re.search(message, failure.llm_view()), failure.llm_view()


class TestServerTraps:
    async def test_fixed_string_with_nul_rolls_back(
        self, dags: PumpDags, postgres: PostgresSide
    ) -> None:
        """FixedString дополняется NUL-байтами, text postgres их не принимает:
        транзакция откатывается, таблицы нет."""
        await postgres.execute([f"drop table if exists {PG_SCHEMA}.fixed"])

        outcome = await dags.run(FIXED_WITH_NUL)

        failure = outcome.failure("pg")
        assert "0x00" in failure.llm_view()
        assert "fixed" not in await Loaded(postgres, PG_SCHEMA, "fixed").tables()

    async def test_fixed_string_trimmed_in_the_query(
        self, dags: PumpDags, clickhouse: ClickHouseSide, postgres: PostgresSide
    ) -> None:
        report = await _landed(dags, clickhouse, postgres, FIXED_TRIMMED)

        assert f"{ROWS} rows loaded" in report
        assert (await Loaded(postgres, PG_SCHEMA, "fixed").texts("fs"))[:1] == ["ab"]


class TestMart:
    async def test_renamed_mart_with_declared_types(
        self, dags: PumpDags, clickhouse: ClickHouseSide, postgres: PostgresSide
    ) -> None:
        report = await _landed(dags, clickhouse, postgres, MART)
        landed = Loaded(postgres, PG_SCHEMA, "mart")

        assert f"{ROWS} rows loaded" in report
        assert await landed.columns() == [
            ("key", "bigint", True),
            ("amount", "numeric(20,2)", True),
            ("label", "character varying(10)", True),
        ]
        assert await landed.texts("label", order_by="key") == await ChLoaded(
            clickhouse, "src"
        ).column("lc")
