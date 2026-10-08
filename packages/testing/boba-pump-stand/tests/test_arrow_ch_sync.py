# ruff: noqa: S608, E501
"""Приёмник ch_stream_in на потоке arrow: postgres, Greenplum, Oracle и ClickHouse
через *_stream_out с wire = arrow в каждый ClickHouse стенда.

Каждый запуск описан toml-текстом (DagSpec) и исполняется DagRunner без
модели: узлы — вызовы насосов с аргументами, как их присылает модель.

Что проверяется:
    - широкая таблица типов postgres по нейтральному контракту: родные типы
      ClickHouse, семейства без своего типа (json, inet, interval, money,
      xml, bit, bytea) — String с текстом источника, значения совпадают;
    - типы без пары по arrow (time, массивы, диапазоны): отказ с подсказкой,
      String по fallback_as_varchar, объявленный тип по column_types;
    - сверка с существующей таблицей по семействам: шире — предупреждение,
      уже — отказ до загрузки;
    - двойник и стратегии на повторной загрузке;
    - Greenplum и Oracle как источники;
    - ClickHouse -> ClickHouse по arrow: что доезжает родным типом, что
      становится строкой, чем arrow уступает tsv.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from decimal import Decimal
from pathlib import Path
from typing import Any, ClassVar

import pytest

from boba.pump_stand import (
    ChLoaded,
    ClickHouseSide,
    Loaded,
    OracleSide,
    PostgresSide,
    PumpDags,
    PumpStand,
)
from boba.pump_stand.oracle import PumpUser
from boba.stand.names import StandNames

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

STAND = PumpStand.required()
PG_SCHEMA = StandNames().of("pump_arrow_ch")
CH_DATABASE = StandNames().of("pump_arrow_ch")
ROWS = 60
SIXTH = ROWS // 6
ARRAYSIZE = 500

SOURCE_DDL = f"""
create table {PG_SCHEMA}.src (
    id bigint not null,
    i2 smallint,
    i4 integer,
    n numeric(18,4),
    nw numeric(38,10),
    f4 real,
    f8 double precision,
    b boolean,
    t text,
    vc varchar(20) not null,
    c5 char(5),
    by bytea,
    d date,
    ts timestamp(6),
    tz timestamptz(3),
    tm time,
    iv interval,
    u uuid,
    j json,
    jb jsonb,
    ip inet,
    mo money,
    bt bit(4),
    xm xml,
    ia integer[],
    tr int4range
)"""

SOURCE_FILL = f"""
insert into {PG_SCHEMA}.src
select g,
       case when g % 7 = 0 then null else (g % 30000)::smallint end,
       case when g % 7 = 0 then null else g * 3 end,
       case when g % 11 = 0 then null else (g * 1.2345)::numeric(18,4) end,
       (g * 12345678.0123456789)::numeric(38,10),
       case when g % 13 = 0 then null else (g / 7.0)::real end,
       case when g % 13 = 0 then null else g / 7.0 end,
       case when g % 5 = 0 then null else g % 2 = 0 end,
       case when g % 9 = 0 then null else 'text ' || g || E' \\t tab' end,
       'label ' || g % 50,
       'ab',
       decode(lpad(to_hex(g), 8, '0'), 'hex'),
       date '2024-01-01' + g % 400,
       timestamp '2024-01-01 00:00:00.123456' + (g || ' minutes')::interval,
       timestamptz '2024-01-01 00:00:00+00' + (g || ' seconds')::interval,
       time '13:14:15' + (g || ' seconds')::interval,
       (g % 90 || ' minutes')::interval,
       md5(g::text)::uuid,
       json_build_object('g', g),
       jsonb_build_object('g', g, 'nested', jsonb_build_object('k', g % 3)),
       ('10.1.' || g % 256 || '.' || g % 250)::inet,
       (g % 1000)::numeric::money,
       B'1010',
       ('<a>' || g || '</a>')::xml,
       array[g, g + 1],
       int4range(g, g + 10)
from generate_series(1, {ROWS}) g"""

EXPECTED_TYPES = {
    "id": "Int64",
    "i2": "Nullable(Int16)",
    "i4": "Nullable(Int32)",
    "n": "Nullable(Decimal(18, 4))",
    "nw": "Nullable(Decimal(38, 10))",
    "f4": "Nullable(Float32)",
    "f8": "Nullable(Float64)",
    "b": "Nullable(Bool)",
    "t": "Nullable(String)",
    "vc": "String",
    "c5": "Nullable(String)",
    "by": "Nullable(String)",
    "d": "Nullable(Date32)",
    "ts": "Nullable(DateTime64(6))",
    "tz": "Nullable(DateTime64(3, 'UTC'))",
    "iv": "Nullable(String)",
    "u": "Nullable(UUID)",
    "j": "Nullable(String)",
    "jb": "Nullable(String)",
    "ip": "Nullable(String)",
    "mo": "Nullable(String)",
    "bt": "Nullable(String)",
    "xm": "Nullable(String)",
}
"""Колонки ClickHouse по нейтральному контракту arrow: семейства без своего
типа у ClickHouse — String; json и inet по arrow тоже String, в отличие от
пары postgres -> ClickHouse по tsv."""

TYPES_CREATE = """
name = "pg_to_ch_arrow_types_create"
version = 1

[[nodes]]
key = "src"
tool = "pg_stream_out"

[nodes.args]
connection = "pg"
sql = "select id, i2, i4, n, nw, f4, f8, b, t, vc, c5, by, d, ts, tz, iv, u, j, jb, ip, mo, bt, xm from $pg_schema.src"
wire = "arrow"
columns = [{ name = "id", nullable = false }, { name = "vc", nullable = false }]
pipe_bytes = 65536
out = "rows"

[[nodes]]
key = "ch"
tool = "ch_stream_in"

[nodes.args]
connection = "ch"
database = "$ch_database"
table_name = "types"
schema_strategy = { kind = "drop_and_create" }
delete_strategy = { kind = "nothing" }
insert_strategy = { kind = "full" }
rules = {}
unknown_types = { kind = "fail_on_unknown" }
create_table = "create table {database}.{table_name}[ on cluster {cluster}] ({columns}) engine = MergeTree order by {order_by}"
order_by = "tuple()"
feed = "rows"
"""

TYPES_DELETE_WHERE = """
name = "pg_to_ch_arrow_types_delete_where"
version = 1

[[nodes]]
key = "src"
tool = "pg_stream_out"

[nodes.args]
connection = "pg"
sql = "select id, i2, i4, n, nw, f4, f8, b, t, vc, c5, by, d, ts, tz, iv, u, j, jb, ip, mo, bt, xm from $pg_schema.src where id <= $sixth"
wire = "arrow"
columns = [{ name = "id", nullable = false }, { name = "vc", nullable = false }]
pipe_bytes = 65536
out = "rows"

[[nodes]]
key = "ch"
tool = "ch_stream_in"

[nodes.args]
connection = "ch"
database = "$ch_database"
table_name = "types"
schema_strategy = { kind = "error_if_schema_changed" }
delete_strategy = { kind = "delete_where", where = "id <= $sixth" }
insert_strategy = { kind = "full" }
rules = {}
unknown_types = { kind = "fail_on_unknown" }
create_table = "create table {database}.{table_name}[ on cluster {cluster}] ({columns}) engine = MergeTree order by {order_by}"
order_by = "tuple()"
feed = "rows"
"""

TYPES_TRUNCATE = """
name = "pg_to_ch_arrow_types_truncate"
version = 1

[[nodes]]
key = "src"
tool = "pg_stream_out"

[nodes.args]
connection = "pg"
sql = "select id, i2, i4, n, nw, f4, f8, b, t, vc, c5, by, d, ts, tz, iv, u, j, jb, ip, mo, bt, xm from $pg_schema.src"
wire = "arrow"
columns = [{ name = "id", nullable = false }, { name = "vc", nullable = false }]
pipe_bytes = 65536
out = "rows"

[[nodes]]
key = "ch"
tool = "ch_stream_in"

[nodes.args]
connection = "ch"
database = "$ch_database"
table_name = "types"
schema_strategy = { kind = "error_if_schema_changed" }
delete_strategy = { kind = "truncate" }
insert_strategy = { kind = "full" }
rules = {}
unknown_types = { kind = "fail_on_unknown" }
create_table = "create table {database}.{table_name}[ on cluster {cluster}] ({columns}) engine = MergeTree order by {order_by}"
order_by = "tuple()"
feed = "rows"
"""

UNKNOWN_TIME = """
name = "pg_to_ch_arrow_unknown_time"
version = 1

[[nodes]]
key = "src"
tool = "pg_stream_out"

[nodes.args]
connection = "pg"
sql = "select id, tm from $pg_schema.src"
wire = "arrow"
columns = [{ name = "id", nullable = false }]
pipe_bytes = 65536
out = "rows"

[[nodes]]
key = "ch"
tool = "ch_stream_in"

[nodes.args]
connection = "ch"
database = "$ch_database"
table_name = "unknown"
schema_strategy = { kind = "drop_and_create" }
delete_strategy = { kind = "nothing" }
insert_strategy = { kind = "full" }
rules = {}
unknown_types = { kind = "fail_on_unknown" }
create_table = "create table {database}.{table_name}[ on cluster {cluster}] ({columns}) engine = MergeTree order by {order_by}"
order_by = "tuple()"
feed = "rows"
"""

UNKNOWN_ARRAY = """
name = "pg_to_ch_arrow_unknown_array"
version = 1

[[nodes]]
key = "src"
tool = "pg_stream_out"

[nodes.args]
connection = "pg"
sql = "select id, ia from $pg_schema.src"
wire = "arrow"
columns = [{ name = "id", nullable = false }]
pipe_bytes = 65536
out = "rows"

[[nodes]]
key = "ch"
tool = "ch_stream_in"

[nodes.args]
connection = "ch"
database = "$ch_database"
table_name = "unknown"
schema_strategy = { kind = "drop_and_create" }
delete_strategy = { kind = "nothing" }
insert_strategy = { kind = "full" }
rules = {}
unknown_types = { kind = "fail_on_unknown" }
create_table = "create table {database}.{table_name}[ on cluster {cluster}] ({columns}) engine = MergeTree order by {order_by}"
order_by = "tuple()"
feed = "rows"
"""

UNKNOWN_RANGE = """
name = "pg_to_ch_arrow_unknown_range"
version = 1

[[nodes]]
key = "src"
tool = "pg_stream_out"

[nodes.args]
connection = "pg"
sql = "select id, tr from $pg_schema.src"
wire = "arrow"
columns = [{ name = "id", nullable = false }]
pipe_bytes = 65536
out = "rows"

[[nodes]]
key = "ch"
tool = "ch_stream_in"

[nodes.args]
connection = "ch"
database = "$ch_database"
table_name = "unknown"
schema_strategy = { kind = "drop_and_create" }
delete_strategy = { kind = "nothing" }
insert_strategy = { kind = "full" }
rules = {}
unknown_types = { kind = "fail_on_unknown" }
create_table = "create table {database}.{table_name}[ on cluster {cluster}] ({columns}) engine = MergeTree order by {order_by}"
order_by = "tuple()"
feed = "rows"
"""

UNKNOWN_AS_STRING = """
name = "pg_to_ch_arrow_unknown_as_string"
version = 1

[[nodes]]
key = "src"
tool = "pg_stream_out"

[nodes.args]
connection = "pg"
sql = "select id, tm, ia, tr from $pg_schema.src"
wire = "arrow"
columns = [{ name = "id", nullable = false }]
pipe_bytes = 65536
out = "rows"

[[nodes]]
key = "ch"
tool = "ch_stream_in"

[nodes.args]
connection = "ch"
database = "$ch_database"
table_name = "unknown"
schema_strategy = { kind = "drop_and_create" }
delete_strategy = { kind = "nothing" }
insert_strategy = { kind = "full" }
rules = {}
unknown_types = { kind = "fallback_as_varchar" }
create_table = "create table {database}.{table_name}[ on cluster {cluster}] ({columns}) engine = MergeTree order by {order_by}"
order_by = "tuple()"
feed = "rows"
"""

DECLARED_TYPES = """
name = "pg_to_ch_arrow_declared_types"
version = 1

[[nodes]]
key = "src"
tool = "pg_stream_out"

[nodes.args]
connection = "pg"
sql = "select id, tm, n from $pg_schema.src"
wire = "arrow"
columns = [{ name = "id", nullable = false }]
pipe_bytes = 65536
out = "rows"

[[nodes]]
key = "ch"
tool = "ch_stream_in"

[nodes.args]
connection = "ch"
database = "$ch_database"
table_name = "declared"
schema_strategy = { kind = "drop_and_create" }
delete_strategy = { kind = "nothing" }
insert_strategy = { kind = "full" }
rules = { column_types = { tm = "LowCardinality(String)", n = "Decimal(20, 6)" } }
unknown_types = { kind = "fail_on_unknown" }
create_table = "create table {database}.{table_name}[ on cluster {cluster}] ({columns}) engine = MergeTree order by {order_by}"
order_by = "tuple()"
feed = "rows"
"""

EXISTING_WIDER = """
name = "pg_to_ch_arrow_existing_wider"
version = 1

[[nodes]]
key = "src"
tool = "pg_stream_out"

[nodes.args]
connection = "pg"
sql = "select id, i4, n, f4, ts, vc from $pg_schema.src"
wire = "arrow"
columns = [{ name = "id", nullable = false }, { name = "vc", nullable = false }]
pipe_bytes = 65536
out = "rows"

[[nodes]]
key = "ch"
tool = "ch_stream_in"

[nodes.args]
connection = "ch"
database = "$ch_database"
table_name = "existing"
schema_strategy = { kind = "error_if_not_exists" }
delete_strategy = { kind = "nothing" }
insert_strategy = { kind = "full" }
rules = {}
unknown_types = { kind = "fail_on_unknown" }
create_table = "create table {database}.{table_name}[ on cluster {cluster}] ({columns}) engine = MergeTree order by {order_by}"
order_by = "tuple()"
feed = "rows"
"""

EXISTING_CHECK = """
name = "pg_to_ch_arrow_existing_check"
version = 1

[[nodes]]
key = "src"
tool = "pg_stream_out"

[nodes.args]
connection = "pg"
sql = "select id, i4, n, f4, ts, vc from $pg_schema.src"
wire = "arrow"
columns = [{ name = "id", nullable = false }, { name = "vc", nullable = false }]
pipe_bytes = 65536
out = "rows"

[[nodes]]
key = "ch"
tool = "ch_stream_in"

[nodes.args]
connection = "ch"
database = "$ch_database"
table_name = "existing"
schema_strategy = { kind = "error_if_schema_changed" }
delete_strategy = { kind = "nothing" }
insert_strategy = { kind = "full" }
rules = {}
unknown_types = { kind = "fail_on_unknown" }
create_table = "create table {database}.{table_name}[ on cluster {cluster}] ({columns}) engine = MergeTree order by {order_by}"
order_by = "tuple()"
feed = "rows"
"""

FROM_ORACLE = """
name = "ora_to_ch_arrow"
version = 1

[[nodes]]
key = "src"
tool = "ora_stream_out"

[nodes.args]
connection = "ora"
sql = 'select id as "id", amount as "amount", name as "name", ts as "ts" from $ora_user.$ora_table'
columns = [{ name = "id", nullable = false }]
out = "rows"

[[nodes]]
key = "ch"
tool = "ch_stream_in"

[nodes.args]
connection = "ch"
database = "$ch_database"
table_name = "from_oracle"
schema_strategy = { kind = "drop_and_create" }
delete_strategy = { kind = "nothing" }
insert_strategy = { kind = "full" }
rules = {}
unknown_types = { kind = "fail_on_unknown" }
create_table = "create table {database}.{table_name}[ on cluster {cluster}] ({columns}) engine = MergeTree order by {order_by}"
order_by = "tuple()"
feed = "rows"
"""

CIRCLE = """
name = "ch_to_ch_arrow_circle"
version = 1

[[nodes]]
key = "src"
tool = "ch_stream_out"

[nodes.args]
connection = "ch"
sql = "select id, u8, u64, i8, f, dec, lc, toString(en) as en, s, toDate32(d) as d, d32, toDateTime64(dt, 0, 'UTC') as dt, dt64, b, toString(big) as big, toString(arr) as arr, toString(m) as m from $ch_database.typed settings output_format_arrow_string_as_string = 1"
wire = "arrow"
columns = [{ name = "id", nullable = false }]
pipe_bytes = 65536
out = "rows"

[[nodes]]
key = "ch"
tool = "ch_stream_in"

[nodes.args]
connection = "ch"
database = "$ch_database"
table_name = "circle"
schema_strategy = { kind = "drop_and_create" }
delete_strategy = { kind = "nothing" }
insert_strategy = { kind = "full" }
rules = {}
unknown_types = { kind = "fail_on_unknown" }
create_table = "create table {database}.{table_name}[ on cluster {cluster}] ({columns}) engine = MergeTree order by {order_by}"
order_by = "id"
feed = "rows"
"""

RAW_DATES = """
name = "ch_to_ch_arrow_raw_dates"
version = 1

[[nodes]]
key = "src"
tool = "ch_stream_out"

[nodes.args]
connection = "ch"
sql = "select id, d, dt from $ch_database.typed settings output_format_arrow_string_as_string = 1"
wire = "arrow"
columns = [{ name = "id", nullable = false }]
pipe_bytes = 65536
out = "rows"

[[nodes]]
key = "ch"
tool = "ch_stream_in"

[nodes.args]
connection = "ch"
database = "$ch_database"
table_name = "raw_dates"
schema_strategy = { kind = "drop_and_create" }
delete_strategy = { kind = "nothing" }
insert_strategy = { kind = "full" }
rules = {}
unknown_types = { kind = "fail_on_unknown" }
create_table = "create table {database}.{table_name}[ on cluster {cluster}] ({columns}) engine = MergeTree order by {order_by}"
order_by = "tuple()"
feed = "rows"
"""


def _named(prefix: str) -> list[Any]:
    chosen: list[Any] = []
    for source in STAND.sources:
        if source.name.startswith(prefix):
            chosen.append(source)

    return chosen


def _sources() -> list[Any]:
    """Новейший postgres и новейший Greenplum."""
    chosen: list[Any] = []
    for prefix in ("pg-", "gp-"):
        named = _named(prefix)
        if named:
            chosen.append(named[-1])

    return chosen


@pytest.fixture(scope="module", params=_sources(), ids=lambda s: s.name)
async def postgres(request: Any) -> AsyncIterator[PostgresSide]:
    side = PostgresSide(request.param, PG_SCHEMA)
    await side.connect()
    await side.recreate_schema([SOURCE_DDL, SOURCE_FILL])

    yield side

    await side.drop()


@pytest.fixture(scope="module", params=STAND.demo_clickhouse(), ids=lambda s: s.name)
async def clickhouse(request: Any) -> AsyncIterator[ClickHouseSide]:
    side = ClickHouseSide(request.param, CH_DATABASE)
    await side.connect()
    await side.recreate_database()

    yield side

    await side.drop()


@pytest.fixture(scope="module")
async def oracle() -> AsyncIterator[OracleSide]:
    side = OracleSide(max(STAND.ora_sources, key=lambda s: s.name), ARRAYSIZE)
    await side.connect()
    await side.recreate_user()

    yield side

    await side.drop()


@pytest.fixture
def dags(
    tmp_path: Path, postgres: PostgresSide, clickhouse: ClickHouseSide
) -> PumpDags:
    return PumpDags(
        tmp_path,
        {"pg": postgres.profile, "ch": clickhouse.profile},
        {"pg_schema": PG_SCHEMA, "ch_database": CH_DATABASE, "sixth": str(SIXTH)},
    )


@pytest.fixture
def ora_dags(
    tmp_path: Path, oracle: OracleSide, clickhouse: ClickHouseSide
) -> PumpDags:
    """Oracle -> ClickHouse: таблица источника своя на каждый ClickHouse."""
    return PumpDags(
        tmp_path,
        {"ora": oracle.profile, "ch": clickhouse.profile},
        {
            "ch_database": CH_DATABASE,
            "ora_user": PumpUser().name,
            "ora_table": f"src_{clickhouse.major}",
        },
    )


@pytest.fixture
def ch_dags(tmp_path: Path, clickhouse: ClickHouseSide) -> PumpDags:
    """Круг ClickHouse -> ClickHouse: соединение одно."""
    return PumpDags(tmp_path, {"ch": clickhouse.profile}, {"ch_database": CH_DATABASE})


async def _landed(dags: PumpDags, clickhouse: ClickHouseSide, spec: str) -> str:
    """Запуск описания; отчёт приёмника — узла ch."""
    outcome = await dags.run(spec)
    report = outcome.node("ch").content
    print(f"\n--- {clickhouse.source.name}: {outcome.dag} ---\n{report}")

    assert outcome.ok(), report

    return report


class TestPostgresTypes:
    async def test_types_and_values_land(
        self, dags: PumpDags, postgres: PostgresSide, clickhouse: ClickHouseSide
    ) -> None:
        report = await _landed(dags, clickhouse, TYPES_CREATE)
        landed = ChLoaded(clickhouse, "types")
        source = Loaded(postgres, PG_SCHEMA, "src")

        assert f"{ROWS} rows loaded" in report
        assert dict(await landed.types()) == EXPECTED_TYPES
        assert await landed.count() == ROWS
        assert Decimal(str(await landed.scalar("sum(n)"))) == await source.aggregate(
            "sum(n)"
        )
        assert Decimal(str(await landed.scalar("sum(nw)"))) == await source.aggregate(
            "sum(nw)"
        )
        assert await landed.scalar("sum(i4)") == await source.aggregate("sum(i4)")
        assert await landed.scalar("countIf(b)") == await source.aggregate(
            "count(*) filter (where b)"
        )
        assert await landed.scalar("countIf(i2 is null)") == await source.aggregate(
            "count(*) filter (where i2 is null)"
        )
        assert await landed.column("t") == await source.texts("t")
        assert await landed.column("toString(u)") == await source.texts("u")
        assert await landed.column("toString(d)") == await source.texts("d")
        assert await landed.column("toString(ts)") == await source.texts("ts")
        assert await landed.column("toString(tz)") == await source.scalars(
            "to_char(tz at time zone 'UTC', 'YYYY-MM-DD HH24:MI:SS.MS')"
        )
        for column in ("j", "jb", "iv", "mo", "bt", "xm", "by"):
            assert await landed.column(column) == await source.texts(column), column
        assert await landed.column("ip") == await source.scalars("host(ip)")

    async def test_reload_through_the_twin(
        self, dags: PumpDags, clickhouse: ClickHouseSide
    ) -> None:
        report = await _landed(dags, clickhouse, TYPES_DELETE_WHERE)
        landed = ChLoaded(clickhouse, "types")

        assert f"{SIXTH} rows matching" in report
        assert f"{SIXTH} rows loaded" in report
        assert await landed.count() == ROWS
        assert await ChLoaded(clickhouse, "types__ex").count() == ROWS

    async def test_truncate_reload(
        self, dags: PumpDags, clickhouse: ClickHouseSide
    ) -> None:
        report = await _landed(dags, clickhouse, TYPES_TRUNCATE)

        assert f"{ROWS} rows loaded" in report
        assert await ChLoaded(clickhouse, "types").count() == ROWS


class TestUnknownTypes:
    """time, массив и диапазон по arrow — семейства без пары у ClickHouse."""

    @pytest.mark.parametrize(
        ("column", "spec"),
        [("tm", UNKNOWN_TIME), ("ia", UNKNOWN_ARRAY), ("tr", UNKNOWN_RANGE)],
    )
    async def test_refused_by_default(
        self, dags: PumpDags, column: str, spec: str
    ) -> None:
        outcome = await dags.run(spec)

        failure = outcome.failure("ch")
        assert failure.error_kind == "TransferError", failure
        assert f"column {column}: the target" in failure.llm_view()

    async def test_land_as_string_on_fallback(
        self, dags: PumpDags, postgres: PostgresSide, clickhouse: ClickHouseSide
    ) -> None:
        report = await _landed(dags, clickhouse, UNKNOWN_AS_STRING)
        landed = ChLoaded(clickhouse, "unknown")
        source = Loaded(postgres, PG_SCHEMA, "src")

        assert f"{ROWS} rows loaded" in report
        assert dict(await landed.types()) == {
            "id": "Int64",
            "tm": "Nullable(String)",
            "ia": "Nullable(String)",
            "tr": "Nullable(String)",
        }
        for column in ("tm", "ia", "tr"):
            assert await landed.column(column) == await source.texts(column), column

    async def test_declared_type_wins(
        self, dags: PumpDags, postgres: PostgresSide, clickhouse: ClickHouseSide
    ) -> None:
        report = await _landed(dags, clickhouse, DECLARED_TYPES)
        landed = ChLoaded(clickhouse, "declared")

        assert f"{ROWS} rows loaded" in report
        assert dict(await landed.types()) == {
            "id": "Int64",
            "tm": "LowCardinality(Nullable(String))",
            "n": "Nullable(Decimal(20, 6))",
        }
        assert await landed.column("tm") == await Loaded(
            postgres, PG_SCHEMA, "src"
        ).texts("tm")


class TestExistingTable:
    """Сверка контракта с существующей таблицей по семействам: шире —
    предупреждение и загрузка, уже — отказ до загрузки."""

    BASE: ClassVar[dict[str, str]] = {
        "id": "Int64",
        "i4": "Nullable(Int32)",
        "n": "Nullable(Decimal(18, 4))",
        "f4": "Nullable(Float32)",
        "ts": "Nullable(DateTime64(6))",
        "vc": "String",
    }

    async def create(self, clickhouse: ClickHouseSide, types: dict[str, str]) -> None:
        await clickhouse.command(f"drop table if exists {CH_DATABASE}.existing")
        columns = ", ".join(f"{name} {kind}" for name, kind in types.items())
        await clickhouse.command(
            f"create table {CH_DATABASE}.existing ({columns}) "
            "engine = MergeTree order by id"
        )

    async def test_wider_table_takes_the_stream(
        self, dags: PumpDags, clickhouse: ClickHouseSide
    ) -> None:
        await self.create(
            clickhouse,
            {
                **self.BASE,
                "id": "Int128",
                "i4": "Nullable(Int64)",
                "n": "Nullable(Decimal(38, 8))",
                "f4": "Nullable(Float64)",
                "ts": "Nullable(DateTime64(9))",
                "vc": "LowCardinality(String)",
            },
        )

        report = await _landed(dags, clickhouse, EXISTING_WIDER)

        assert f"{ROWS} rows loaded" in report
        assert re.search(
            r'"column": "i4",[^}]*"note": "table [^"]+ is wider than', report
        )
        assert re.search(
            r'"column": "n",[^}]*"note": "table [^"]+ is wider than', report
        )
        assert re.search(
            r'"column": "ts",[^}]*"note": "table [^"]+ is finer than', report
        )
        assert await ChLoaded(clickhouse, "existing").count() == ROWS

    @pytest.mark.parametrize(
        ("column", "table_type", "message"),
        [
            ("i4", "Nullable(Int16)", "is narrower than"),
            ("i4", "Nullable(UInt32)", "has no sign"),
            ("n", "Nullable(Decimal(18, 2))", "truncates the scale"),
            ("n", "Nullable(Decimal(10, 4))", "fewer integer digits"),
            ("ts", "Nullable(DateTime64(3))", "is coarser than"),
            ("ts", "Nullable(DateTime64(6, 'UTC'))", "time zone differs"),
            ("vc", "Int64", "type family differs"),
            ("i4", "Int32", "stream is nullable"),
        ],
    )
    async def test_narrower_table_is_refused(
        self,
        dags: PumpDags,
        clickhouse: ClickHouseSide,
        column: str,
        table_type: str,
        message: str,
    ) -> None:
        await self.create(clickhouse, {**self.BASE, column: table_type})

        outcome = await dags.run(EXISTING_CHECK)

        failure = outcome.failure("ch")
        assert failure.error_kind == "TransferError", failure
        assert re.search(f"{column}.*{message}", failure.llm_view()), failure.llm_view()


class TestOracleSource:
    """Oracle -> ClickHouse через ora_stream_out и ch_stream_in: целый NUMBER до 18
    знаков — Int64, NUMBER с дробью — Decimal, VARCHAR2 — String, DATE и
    TIMESTAMP — DateTime64."""

    async def test_oracle_lands(
        self, ora_dags: PumpDags, oracle: OracleSide, clickhouse: ClickHouseSide
    ) -> None:
        table = f"src_{clickhouse.major}"
        await oracle.create(
            table,
            [
                "id number(10) not null",
                "amount number(18,4)",
                "name varchar2(50)",
                "ts timestamp(6)",
            ],
        )
        await oracle.run(
            (
                f"insert into {PumpUser().name}.{table} select level, level / 7, "
                "'n' || level, timestamp '2024-01-01 00:00:00' "
                "+ numtodsinterval(level, 'second') from dual "
                f"connect by level <= {ROWS}",
            )
        )

        report = await _landed(ora_dags, clickhouse, FROM_ORACLE)
        landed = ChLoaded(clickhouse, "from_oracle")
        types = dict(await landed.types())

        assert f"{ROWS} rows loaded" in report
        assert types["id"] == "Int64"
        assert types["amount"] == "Nullable(Decimal(18, 4))"
        assert types["name"] == "Nullable(String)"
        assert types["ts"].startswith("Nullable(DateTime64(")
        assert await landed.count() == ROWS
        assert await landed.scalar("sum(id)") == ROWS * (ROWS + 1) // 2
        assert (await landed.column("name"))[:2] == ["n1", "n2"]


class TestClickHouseCircle:
    """ClickHouse -> ClickHouse по arrow: родные типы Arrow доезжают своим
    типом; LowCardinality становится String, Enum — String, DateTime64 с
    поясом — момент в UTC; составные и широкие целые Arrow не несёт — их
    select переводит в строку. Для точной передачи типов ClickHouse есть tsv."""

    TYPED: ClassVar[str] = f"""
        create table {CH_DATABASE}.typed (
            id Int64,
            u8 UInt8,
            u64 UInt64,
            i8 Int8,
            f Float32,
            dec Decimal(18, 4),
            lc LowCardinality(String),
            en Enum8('a' = 1, 'b' = 2),
            s Nullable(String),
            d Date,
            d32 Date32,
            dt DateTime,
            dt64 DateTime64(3, 'Europe/Moscow'),
            b Bool,
            big Int128,
            arr Array(Int32),
            m Map(String, UInt64)
        ) engine = MergeTree order by id"""
    FILL: ClassVar[str] = f"""
        insert into {CH_DATABASE}.typed
        select number, number % 200, number * 1000000007, toInt8(number % 100 - 50),
               number / 3, toDecimal64(number, 4) / 7,
               concat('lc', toString(number % 3)), if(number % 2 = 0, 'a', 'b'),
               if(number % 5 = 0, null, concat('s', toString(number))),
               toDate('2024-01-01') + number % 300,
               toDate32('1950-01-01') + number % 300,
               toDateTime('2024-02-29 13:14:15', 'UTC') + number,
               toDateTime64('2024-02-29 13:14:15.123', 3, 'Europe/Moscow') + number,
               number % 2 = 0, toInt128(number) * 1000000000000,
               [toInt32(number), 1], map('k', number)
        from numbers({ROWS})"""
    ARROW_DATE_SINCE: ClassVar[int] = 26
    ARROW_BOOL_SINCE: ClassVar[int] = 23

    async def test_types_come_around(
        self, ch_dags: PumpDags, clickhouse: ClickHouseSide
    ) -> None:
        await clickhouse.command(f"drop table if exists {CH_DATABASE}.typed")
        await clickhouse.command(self.TYPED)
        await clickhouse.command(self.FILL)

        report = await _landed(ch_dags, clickhouse, CIRCLE)
        circle = ChLoaded(clickhouse, "circle")
        typed = ChLoaded(clickhouse, "typed")
        types = dict(await circle.types())

        assert f"{ROWS} rows loaded" in report
        assert types["id"] == "Int64"
        assert types["u64"] == "UInt64"
        assert types["dec"] == "Decimal(18, 4)"
        assert types["lc"] == "String"
        assert types["s"] == "Nullable(String)"
        assert await circle.count() == ROWS
        for expression in (
            "u8",
            "u64",
            "i8",
            "toString(dec)",
            "lc",
            "s",
            "toString(d)",
            "toString(d32)",
            "toUInt8(b)",
        ):
            assert await circle.column(expression) == await typed.column(expression), (
                expression
            )
        assert await circle.column(
            "toUnixTimestamp64Milli(dt64)"
        ) == await typed.column("toUnixTimestamp64Milli(dt64)")
        assert await circle.column("toUnixTimestamp(dt)") == await typed.column(
            "toUnixTimestamp(dt)"
        )
        assert types["d"] == "Date32"
        assert types["dt"] == "DateTime64(0, 'UTC')"
        if clickhouse.major < self.ARROW_BOOL_SINCE:
            assert types["b"] == "UInt8"
        else:
            assert types["b"] == "Bool"
        for expression in ("en", "big", "arr", "m"):
            assert await circle.column(expression) == await typed.column(
                f"toString({expression})"
            ), expression

    async def test_dates_without_cast_become_numbers(
        self, ch_dags: PumpDags, clickhouse: ClickHouseSide
    ) -> None:
        """Ловушка: Date до 26 и DateTime на всех версиях уходят в Arrow целыми,
        контракт видит UInt16 и UInt32 — на приёмнике это числа; Bool на 22.12
        тем же образом становится UInt8."""
        await _landed(ch_dags, clickhouse, RAW_DATES)
        types = dict(await ChLoaded(clickhouse, "raw_dates").types())

        assert types["dt"] == "UInt32"
        if clickhouse.major < self.ARROW_DATE_SINCE:
            assert types["d"] == "UInt16"
        else:
            assert types["d"] == "Date32"
