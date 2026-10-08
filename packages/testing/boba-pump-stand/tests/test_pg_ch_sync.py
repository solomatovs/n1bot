# ruff: noqa: S608, E501
"""Пара postgres -> ClickHouse по tsv: pg_stream_out с wire = tsv в ch_stream_in на
каждом ClickHouse стенда, источник — новейший postgres с таблицей всех
ходовых типов.

Каждый запуск описан toml-текстом (DagSpec) и исполняется DagRunner без
модели: узлы — вызовы насосов с аргументами, как их присылает модель.

Что проверяется:
    - каждый тип postgres ложится своим типом ClickHouse, значения совпадают
      текстом; json и jsonb — JSON, где сервер его умеет, nullable jsonb —
      Nullable(JSON) только с 25.x, иначе String;
    - типы без пары (массивы, numeric без точности или шире 76 разрядов,
      enum) — отказ с подсказкой или String по fallback_as_varchar;
    - ловушки сервера: inet с маской и массив на верхнем уровне json — ошибка
      при загрузке, таблица не меняется; даты вне 1900–2299 прижимаются;
    - повторная загрузка со сверкой, витрина через rename и column_types.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from decimal import Decimal
from pathlib import Path
from typing import Any

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
from boba.toolkit.result import SqlFailureResult

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

STAND = PumpStand.required()
PG_SCHEMA = StandNames().of("pump_pg_ch")
CH_DATABASE = StandNames().of("pump_pg_ch")
ROWS = 60
NULLABLE_JSON_SINCE = 25
IPV4_IN_IPV6_SINCE = 23

SOURCE_DDL = f"""
create table {PG_SCHEMA}.src (
    id bigint not null,
    i2 smallint,
    i4 integer,
    n numeric(18,4),
    nfree numeric,
    nwide numeric(80,2),
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
    jb jsonb not null,
    ip inet,
    net cidr,
    mac macaddr,
    mo money,
    bt bit(4),
    xm xml,
    ia integer[],
    en mood,
    tr int4range
)"""

SOURCE_FILL = f"""
insert into {PG_SCHEMA}.src
select g,
       case when g % 7 = 0 then null else (g % 30000)::smallint end,
       case when g % 7 = 0 then null else g * 3 end,
       case when g % 11 = 0 then null else (g * 1.2345)::numeric(18,4) end,
       (g * 1.5)::numeric,
       (g * 1e60)::numeric(80,2),
       case when g % 13 = 0 then null else (g / 7.0)::real end,
       case when g % 13 = 0 then null else g / 7.0 end,
       case when g % 5 = 0 then null else g % 2 = 0 end,
       case when g % 9 = 0 then null else 'text ' || g || E' \\t tab \\\\ back' end,
       'label ' || g % 50,
       'ab',
       decode(lpad(to_hex(g), 8, '0'), 'hex'),
       date '2024-01-01' + g % 400,
       timestamp '2024-01-01 00:00:00.123456' + (g || ' minutes')::interval,
       timestamptz '2024-01-01 00:00:00+00' + (g || ' seconds')::interval,
       time '13:14:15' + (g || ' seconds')::interval,
       (g % 90 || ' minutes')::interval,
       md5(g::text)::uuid,
       case when g % 3 = 0 then null
            else json_build_object('g', g, 'tag', 'x' || g % 3) end,
       jsonb_build_object('g', g, 'nested', jsonb_build_object('k', g % 3)),
       ('10.1.' || g % 256 || '.' || g % 250)::inet,
       '10.0.0.0/8'::cidr,
       '08:00:2b:01:02:03'::macaddr,
       (g % 1000)::numeric::money,
       B'1010',
       ('<a>' || g || '</a>')::xml,
       array[g, g + 1],
       (array['sad', 'happy'])[1 + g % 2]::mood,
       int4range(g, g + 10)
from generate_series(1, {ROWS}) g"""

EXPECTED_TYPES = {
    "id": "Int64",
    "i2": "Nullable(Int16)",
    "i4": "Nullable(Int32)",
    "n": "Nullable(Decimal(18, 4))",
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
    "tm": "Nullable(String)",
    "iv": "Nullable(String)",
    "u": "Nullable(UUID)",
    "net": "Nullable(String)",
    "mac": "Nullable(String)",
    "mo": "Nullable(String)",
    "bt": "Nullable(String)",
    "xm": "Nullable(String)",
    "tr": "Nullable(String)",
}
"""Типы ClickHouse по контракту postgres, кроме json и jsonb: они зависят от
версии сервера."""

NATIVE_TYPES = """
name = "pg_to_ch_native_types"
version = 1

[[nodes]]
key = "src"
tool = "pg_stream_out"

[nodes.args]
connection = "pg"
sql = "select id, i2, i4, n, f4, f8, b, t, vc, c5, by, d, ts, tz, tm, iv, u, j, jb, ip, net, mac, mo, bt, xm, tr from $pg_schema.src"
wire = "tsv"
columns = [{ name = "id", nullable = false }, { name = "vc", nullable = false }, { name = "jb", nullable = false }]
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
order_by = "id"
feed = "rows"
"""

RELOAD = """
name = "pg_to_ch_reload"
version = 1

[[nodes]]
key = "src"
tool = "pg_stream_out"

[nodes.args]
connection = "pg"
sql = "select id, i2, i4, n, f4, f8, b, t, vc, c5, by, d, ts, tz, tm, iv, u, j, jb, ip, net, mac, mo, bt, xm, tr from $pg_schema.src"
wire = "tsv"
columns = [{ name = "id", nullable = false }, { name = "vc", nullable = false }, { name = "jb", nullable = false }]
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
order_by = "id"
feed = "rows"
"""

WIDER = """
name = "pg_to_ch_wider_table"
version = 1

[[nodes]]
key = "src"
tool = "pg_stream_out"

[nodes.args]
connection = "pg"
sql = "select id, i4, n, tz, vc from $pg_schema.src"
wire = "tsv"
columns = [{ name = "id", nullable = false }, { name = "vc", nullable = false }]
pipe_bytes = 65536
out = "rows"

[[nodes]]
key = "ch"
tool = "ch_stream_in"

[nodes.args]
connection = "ch"
database = "$ch_database"
table_name = "wider"
schema_strategy = { kind = "error_if_not_exists" }
delete_strategy = { kind = "nothing" }
insert_strategy = { kind = "full" }
rules = {}
unknown_types = { kind = "fail_on_unknown" }
create_table = "create table {database}.{table_name}[ on cluster {cluster}] ({columns}) engine = MergeTree order by {order_by}"
order_by = "id"
feed = "rows"
"""

UNKNOWN_REFUSED = """
name = "pg_to_ch_unknown_refused"
version = 1

[[nodes]]
key = "src"
tool = "pg_stream_out"

[nodes.args]
connection = "pg"
sql = "select id, nfree, nwide, ia, en from $pg_schema.src"
wire = "tsv"
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
order_by = "id"
feed = "rows"
"""

UNKNOWN_AS_STRING = """
name = "pg_to_ch_unknown_as_string"
version = 1

[[nodes]]
key = "src"
tool = "pg_stream_out"

[nodes.args]
connection = "pg"
sql = "select id, nfree, nwide, ia, en from $pg_schema.src"
wire = "tsv"
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
order_by = "id"
feed = "rows"
"""

DECLARED_TYPES = """
name = "pg_to_ch_declared_types"
version = 1

[[nodes]]
key = "src"
tool = "pg_stream_out"

[nodes.args]
connection = "pg"
sql = "select id, nfree, en from $pg_schema.src"
wire = "tsv"
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
rules = { column_types = { nfree = "Decimal(38, 4)", en = "Enum8('sad' = 1, 'happy' = 2)" } }
unknown_types = { kind = "fail_on_unknown" }
create_table = "create table {database}.{table_name}[ on cluster {cluster}] ({columns}) engine = MergeTree order by {order_by}"
order_by = "id"
feed = "rows"
"""

NULLABLE_KEY = """
name = "pg_to_ch_nullable_sorting_key"
version = 1

[[nodes]]
key = "src"
tool = "pg_stream_out"

[nodes.args]
connection = "pg"
sql = "select id, i2, i4, n, f4, f8, b, t, vc, c5, by, d, ts, tz, tm, iv, u, j, jb, ip, net, mac, mo, bt, xm, tr from $pg_schema.src"
wire = "tsv"
out = "rows"

[[nodes]]
key = "ch"
tool = "ch_stream_in"

[nodes.args]
connection = "ch"
database = "$ch_database"
table_name = "nullable_key"
schema_strategy = { kind = "create_if_not_exists" }
delete_strategy = { kind = "nothing" }
insert_strategy = { kind = "full" }
unknown_types = { kind = "fallback_as_varchar" }
create_table = "create table {database}.{table_name} ({columns}) engine = MergeTree order by {order_by}"
order_by = "id"
feed = "rows"
"""

MASKED_INET = """
name = "pg_to_ch_masked_inet"
version = 1

[[nodes]]
key = "src"
tool = "pg_stream_out"

[nodes.args]
connection = "pg"
sql = "select id, '10.0.0.0/8'::inet as ip from $pg_schema.src"
wire = "tsv"
columns = [{ name = "id", nullable = false }]
pipe_bytes = 65536
out = "rows"

[[nodes]]
key = "ch"
tool = "ch_stream_in"

[nodes.args]
connection = "ch"
database = "$ch_database"
table_name = "masked"
schema_strategy = { kind = "drop_and_create" }
delete_strategy = { kind = "nothing" }
insert_strategy = { kind = "full" }
rules = {}
unknown_types = { kind = "fail_on_unknown" }
create_table = "create table {database}.{table_name}[ on cluster {cluster}] ({columns}) engine = MergeTree order by {order_by}"
order_by = "id"
feed = "rows"
"""

JSON_ARRAY = """
name = "pg_to_ch_json_array_on_top"
version = 1

[[nodes]]
key = "src"
tool = "pg_stream_out"

[nodes.args]
connection = "pg"
sql = "select id, '[1, 2]'::jsonb as jb from $pg_schema.src"
wire = "tsv"
columns = [{ name = "id", nullable = false }, { name = "jb", nullable = false }]
pipe_bytes = 65536
out = "rows"

[[nodes]]
key = "ch"
tool = "ch_stream_in"

[nodes.args]
connection = "ch"
database = "$ch_database"
table_name = "json_array"
schema_strategy = { kind = "drop_and_create" }
delete_strategy = { kind = "nothing" }
insert_strategy = { kind = "full" }
rules = {}
unknown_types = { kind = "fail_on_unknown" }
create_table = "create table {database}.{table_name}[ on cluster {cluster}] ({columns}) engine = MergeTree order by {order_by}"
order_by = "id"
feed = "rows"
"""

CLAMPED_DATES = """
name = "pg_to_ch_clamped_dates"
version = 1

[[nodes]]
key = "src"
tool = "pg_stream_out"

[nodes.args]
connection = "pg"
sql = "select id, date '1800-06-15' as d, timestamp '2500-01-01 00:00:00' as ts from $pg_schema.src limit 1"
wire = "tsv"
columns = [{ name = "id", nullable = false }]
pipe_bytes = 65536
out = "rows"

[[nodes]]
key = "ch"
tool = "ch_stream_in"

[nodes.args]
connection = "ch"
database = "$ch_database"
table_name = "clamped"
schema_strategy = { kind = "drop_and_create" }
delete_strategy = { kind = "nothing" }
insert_strategy = { kind = "full" }
rules = {}
unknown_types = { kind = "fail_on_unknown" }
create_table = "create table {database}.{table_name}[ on cluster {cluster}] ({columns}) engine = MergeTree order by {order_by}"
order_by = "id"
feed = "rows"
"""

MART = """
name = "pg_to_ch_mart"
version = 1

[[nodes]]
key = "src"
tool = "pg_stream_out"

[nodes.args]
connection = "pg"
sql = "select id, n, ip from $pg_schema.src"
wire = "tsv"
columns = [{ name = "id", nullable = false }]
pipe_bytes = 65536
out = "rows"

[[nodes]]
key = "ch"
tool = "ch_stream_in"

[nodes.args]
connection = "ch"
database = "$ch_database"
table_name = "mart"
schema_strategy = { kind = "drop_and_create" }
delete_strategy = { kind = "nothing" }
insert_strategy = { kind = "full" }
rules = { rename_columns = { key = "id", amount = "n", address = "ip" }, column_types = { amount = "Decimal(20, 4)", address = "String" } }
unknown_types = { kind = "fail_on_unknown" }
create_table = "create table {database}.{table_name}[ on cluster {cluster}] ({columns}) engine = MergeTree order by {order_by}"
order_by = "key"
feed = "rows"
"""


def _newest_postgres() -> Any:
    newest: Any = None
    for source in STAND.sources:
        if source.name.startswith("pg-"):
            newest = source

    if newest is None:
        raise AssertionError("no postgres source on the stand")

    return newest


@pytest.fixture(scope="module")
async def postgres() -> AsyncIterator[PostgresSide]:
    side = PostgresSide(_newest_postgres(), PG_SCHEMA)
    await side.connect()
    await side.recreate_schema(
        ["create type mood as enum ('sad', 'happy')", SOURCE_DDL, SOURCE_FILL]
    )

    yield side

    await side.drop()


@pytest.fixture(scope="module", params=STAND.demo_clickhouse(), ids=lambda s: s.name)
async def clickhouse(request: Any) -> AsyncIterator[ClickHouseSide]:
    side = ClickHouseSide(request.param, CH_DATABASE)
    await side.connect()
    await side.recreate_database()

    yield side

    await side.drop()


@pytest.fixture
def dags(
    tmp_path: Path, postgres: PostgresSide, clickhouse: ClickHouseSide
) -> PumpDags:
    return PumpDags(
        tmp_path,
        {"pg": postgres.profile, "ch": clickhouse.profile},
        {"pg_schema": PG_SCHEMA, "ch_database": CH_DATABASE},
    )


async def _landed(dags: PumpDags, clickhouse: ClickHouseSide, spec: str) -> str:
    """Запуск описания; отчёт приёмника — узла ch."""
    outcome = await dags.run(spec)
    report = outcome.node("ch").content
    print(f"\n--- {clickhouse.source.name}: {outcome.dag} ---\n{report}")

    assert outcome.ok(), report

    return report


def inet_type(clickhouse: ClickHouseSide) -> dict[str, str]:
    """inet postgres: IPv6, где TabSeparated читает текст IPv4 в IPv6, иначе
    String."""
    if clickhouse.major < IPV4_IN_IPV6_SINCE:
        return {"ip": "Nullable(String)"}

    return {"ip": "Nullable(IPv6)"}


def json_types(clickhouse: ClickHouseSide) -> dict[str, str]:
    """Типы json-колонок по версии сервера: до 24 — Object('json'), nullable
    JSON только с 25."""
    if clickhouse.major < 24:
        return {"jb": "Object('json')", "j": "Nullable(String)"}

    if clickhouse.major < NULLABLE_JSON_SINCE:
        return {"jb": "JSON", "j": "Nullable(String)"}

    return {"jb": "JSON", "j": "Nullable(JSON)"}


class TestNativeTypes:
    async def test_types_and_values_land(
        self, dags: PumpDags, postgres: PostgresSide, clickhouse: ClickHouseSide
    ) -> None:
        report = await _landed(dags, clickhouse, NATIVE_TYPES)
        landed = ChLoaded(clickhouse, "types")
        source = Loaded(postgres, PG_SCHEMA, "src")

        assert f"{ROWS} rows loaded" in report
        assert dict(await landed.types()) == {
            **EXPECTED_TYPES,
            **json_types(clickhouse),
            **inet_type(clickhouse),
        }
        assert await landed.count() == ROWS
        assert Decimal(str(await landed.scalar("sum(n)"))) == await source.aggregate(
            "sum(n)"
        )
        assert await landed.scalar("sum(i4)") == await source.aggregate("sum(i4)")
        assert await landed.scalar("countIf(b)") == await source.aggregate(
            "count(*) filter (where b)"
        )
        assert await landed.scalar("countIf(b is null)") == await source.aggregate(
            "count(*) filter (where b is null)"
        )
        assert await landed.column("t") == await source.texts("t")
        assert await landed.column("c5") == await source.scalars("rpad(c5::text, 5)")
        assert await landed.column("toString(u)") == await source.texts("u")
        assert await landed.column("toString(d)") == await source.texts("d")
        assert await landed.column("toString(ts)") == await source.texts("ts")
        assert await landed.column("toString(tz)") == await source.scalars(
            "to_char(tz at time zone 'UTC', 'YYYY-MM-DD HH24:MI:SS.MS')"
        )
        if clickhouse.major >= IPV4_IN_IPV6_SINCE:
            assert await landed.column("toString(ip)") == await source.scalars(
                "'::ffff:' || host(ip)"
            )
        else:
            assert await landed.column("ip") == await source.scalars("host(ip)")
        assert await landed.column("by") == await source.texts("by")
        assert await landed.column("iv") == await source.texts("iv")
        assert await landed.column("tr") == await source.texts("tr")
        assert await landed.column("bt") == await source.texts("bt")

    async def test_reload_passes_the_check(
        self, dags: PumpDags, clickhouse: ClickHouseSide
    ) -> None:
        report = await _landed(dags, clickhouse, RELOAD)

        assert f"{ROWS} rows loaded" in report
        assert await ChLoaded(clickhouse, "types").count() == ROWS

    async def test_wider_table_takes_the_stream(
        self, dags: PumpDags, clickhouse: ClickHouseSide
    ) -> None:
        await clickhouse.command(f"drop table if exists {CH_DATABASE}.wider")
        await clickhouse.command(
            f"create table {CH_DATABASE}.wider (id Int128, i4 Nullable(Int64), "
            "n Nullable(Decimal(38, 6)), tz Nullable(DateTime64(6, 'UTC')), "
            "vc LowCardinality(String)) engine = MergeTree order by id"
        )

        report = await _landed(dags, clickhouse, WIDER)

        assert f"{ROWS} rows loaded" in report
        assert '"column": "id"' in report
        assert "table Int128 is wider than stream Int64" in report
        assert await ChLoaded(clickhouse, "wider").count() == ROWS


class TestUnknownTypes:
    """Массив, numeric без точности, numeric(80, 2) и enum: у ClickHouse нет
    типа, который прочитает текст postgres без потерь."""

    async def test_refused_by_default(self, dags: PumpDags) -> None:
        outcome = await dags.run(UNKNOWN_REFUSED)

        failure = outcome.failure("ch")
        assert "column nfree: the target clickhouse" in failure.llm_view()

    async def test_land_as_string_on_fallback(
        self, dags: PumpDags, postgres: PostgresSide, clickhouse: ClickHouseSide
    ) -> None:
        report = await _landed(dags, clickhouse, UNKNOWN_AS_STRING)
        landed = ChLoaded(clickhouse, "unknown")
        source = Loaded(postgres, PG_SCHEMA, "src")

        assert f"{ROWS} rows loaded" in report
        assert dict(await landed.types()) == {
            "id": "Int64",
            "nfree": "Nullable(String)",
            "nwide": "Nullable(String)",
            "ia": "Nullable(String)",
            "en": "Nullable(String)",
        }
        assert await landed.column("ia") == await source.texts("ia")
        assert await landed.column("nwide") == await source.texts("nwide")
        assert await landed.column("en") == await source.texts("en")

    async def test_declared_type_wins(
        self, dags: PumpDags, postgres: PostgresSide, clickhouse: ClickHouseSide
    ) -> None:
        report = await _landed(dags, clickhouse, DECLARED_TYPES)
        landed = ChLoaded(clickhouse, "declared")

        assert f"{ROWS} rows loaded" in report
        assert dict(await landed.types()) == {
            "id": "Int64",
            "nfree": "Nullable(Decimal(38, 4))",
            "en": "Nullable(Enum8('sad' = 1, 'happy' = 2))",
        }
        assert await landed.column("toString(en)") == await Loaded(
            postgres, PG_SCHEMA, "src"
        ).texts("en")


class TestFailureView:
    """Сбой приёмника в чате: ошибка сервера, колонки, с которыми шла
    загрузка, и все команды вплоть до упавшей."""

    async def test_nullable_sorting_key_shows_columns_and_the_create(
        self, dags: PumpDags
    ) -> None:
        outcome = await dags.run(NULLABLE_KEY)

        failure = outcome.failure("ch")
        assert isinstance(failure, SqlFailureResult), failure

        rows = {row["column"]: row for row in failure.columns}
        assert rows["id"]["type"] == "Nullable(Int64)", rows["id"]
        assert rows["id"]["nullable"] == "nullable"

        create = failure.statements[-1]
        assert create.text.startswith(f"create table `{CH_DATABASE}`.`nullable_key`")
        assert "`id` Nullable(Int64)" in create.text
        assert create.status == "failed: ClickHouseQueryError"

        markdown = failure.chat_view().markdown
        assert "Sorting key contains nullable columns" in markdown
        assert "| id " in markdown
        assert "```sql\ncreate table" in markdown


class TestServerTraps:
    """Что ловит или молча меняет сам сервер ClickHouse при чтении текста
    postgres: маска в inet и массив на верхнем уровне json — ошибка загрузки,
    таблица не меняется; даты вне 1900–2299 прижимаются к границе."""

    async def test_masked_inet_fails_at_load(
        self, dags: PumpDags, clickhouse: ClickHouseSide
    ) -> None:
        if clickhouse.major < IPV4_IN_IPV6_SINCE:
            pytest.skip("inet lands as String on this server")

        outcome = await dags.run(MASKED_INET)

        failure = outcome.failure("ch")
        assert isinstance(failure, SqlFailureResult), failure
        assert "Cannot parse" in failure.chat_view().markdown
        assert await ChLoaded(clickhouse, "masked").count() == 0

    async def test_json_array_on_top_fails_at_load(
        self, dags: PumpDags, clickhouse: ClickHouseSide
    ) -> None:
        outcome = await dags.run(JSON_ARRAY)

        failure = outcome.failure("ch")
        assert isinstance(failure, SqlFailureResult), failure
        assert failure.statements[-1].status == "failed: ClickHouseQueryError"
        assert await ChLoaded(clickhouse, "json_array").count() == 0

    async def test_dates_outside_the_range_are_clamped(
        self, dags: PumpDags, clickhouse: ClickHouseSide
    ) -> None:
        await _landed(dags, clickhouse, CLAMPED_DATES)
        landed = ChLoaded(clickhouse, "clamped")

        assert await landed.column("toString(d)") == ["1900-01-01"]
        assert (await landed.column("toString(ts)"))[0].startswith("2299-12-31")


class TestMart:
    async def test_renamed_mart_with_declared_types(
        self, dags: PumpDags, postgres: PostgresSide, clickhouse: ClickHouseSide
    ) -> None:
        report = await _landed(dags, clickhouse, MART)
        landed = ChLoaded(clickhouse, "mart")
        source = Loaded(postgres, PG_SCHEMA, "src")

        assert f"{ROWS} rows loaded" in report
        assert dict(await landed.types()) == {
            "key": "Int64",
            "amount": "Nullable(Decimal(20, 4))",
            "address": "Nullable(String)",
        }
        assert await landed.column("address", order_by="key") == await source.scalars(
            "host(ip)"
        )
        assert Decimal(str(await landed.scalar("sum(amount)"))) == (
            await source.aggregate("sum(n)")
        )
