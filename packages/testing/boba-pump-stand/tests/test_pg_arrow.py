# ruff: noqa: S608
"""Перекачка из PostgreSQL потоком Arrow IPC: pg_stream_out против pg_stream_in
(круг на каждом postgres и Greenplum из sources), ch_stream_in (в каждый
ClickHouse из ch_sources) и ora_stream_in (в каждый Oracle из ora_sources).

Каждый запуск описан toml-текстом (DagSpec) и исполняется DagRunner без
модели: узлы — вызовы насосов с аргументами, как их присылает модель; список
колонок select зависит от версии сервера и подставляется в описание именем
$select.

Таблица postgres несёт все семейства типов: целые, numeric с точностью и без,
float с NaN и бесконечностями, boolean, text со спецсимволами и char, bytea,
date, timestamp, timestamptz, time, interval, uuid, массивы, json/jsonb,
inet, enum, композит, диапазон, money, bit, геометрию и xml. У каждой
колонки записано, каким типом она едет в Arrow (описание стейтмента у libpq
без выполнения), что писать в select под конкретный приёмник и как
сверяется; всё, чего CSV не несёт (bytea, массивы, time, interval, uuid,
json, составные), едет текстом сервера — по каталогу видно, какие типы едут
только строкой. Отдельные тесты фиксируют ловушки: numeric без точности и
шире 38 знаков отвергаются до выполнения, списки в потоке — до загрузки,
float печатается точно на любой версии сервера, сессия COPY не зависит от
настроек профиля."""

from __future__ import annotations

import io
import struct
import time
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, ClassVar

import pyarrow
import pyarrow.ipc
import pytest

from boba.db.postgres.connection import CopyOptions
from boba.pump_stand import (
    ClickHouseSide,
    OracleSide,
    PostgresSide,
    PumpDags,
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
    Values,
)
from boba.pump_stand.matrix import (
    Target,
    compared,
    exported,
    first,
)
from boba.pump_stand.oracle import PumpUser
from boba.pump_stand.ports import Feed
from boba.stand.collection import StandServers
from boba.stand.names import StandNames
from boba.toolkit.arrow import ArrowColumns
from boba.toolkit.contract import ArrowContract, StreamContract
from boba.toolkit.transfer import Engine, SchemaHead, StreamWire

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

STAND = PumpStand.required()


NEWEST = STAND.newest_postgres()
ON_NEWEST = StandServers().only("postgres", [NEWEST])
"""Случай не зависит от версии сервера: идёт на одном, самом новом postgres."""
ROWS = 60
ARRAYSIZE = 97
CHUNK_BYTES = 4096
PG_SCHEMA = StandNames().of("pump_pg_arrow")
CH_DATABASE = StandNames().of("pump_pg_arrow")
NULL_EVERY = 7

CIRCLE = """
name = "pg_arrow_circle"
version = 1

[[nodes]]
key = "src"
tool = "pg_stream_out"

[nodes.args]
connection = "pg"
sql = "$select"
wire = "arrow"
pipe_bytes = 4096
out = "rows"

[[nodes]]
key = "dst"
tool = "pg_stream_in"

[nodes.args]
connection = "pg"
schema_name = "$pg_schema"
table_name = "dst"
schema_strategy = { kind = "create_if_not_exists" }
delete_strategy = { kind = "nothing" }
insert_strategy = { kind = "full" }
feed = "rows"
"""

TO_CLICKHOUSE = """
name = "pg_arrow_to_clickhouse"
version = 1

[[nodes]]
key = "src"
tool = "pg_stream_out"

[nodes.args]
connection = "pg"
sql = "$select"
wire = "arrow"
pipe_bytes = 4096
out = "rows"

[[nodes]]
key = "dst"
tool = "ch_stream_in"

[nodes.args]
connection = "ch"
database = "$ch_database"
table_name = "$table"
schema_strategy = { kind = "error_if_not_exists" }
delete_strategy = { kind = "nothing" }
insert_strategy = { kind = "full" }
feed = "rows"
"""

TO_ORACLE = """
name = "pg_arrow_to_oracle"
version = 1

[[nodes]]
key = "src"
tool = "pg_stream_out"

[nodes.args]
connection = "pg"
sql = "$select"
wire = "arrow"
pipe_bytes = 4096
out = "rows"

[[nodes]]
key = "dst"
tool = "ora_stream_in"

[nodes.args]
connection = "ora"
schema_name = "$ora_schema"
table_name = "$table"
schema_strategy = { kind = "error_if_not_exists" }
delete_strategy = { kind = "nothing" }
insert_strategy = { kind = "full" }
feed = "rows"
"""

UNBOUNDED_NUMERIC = """
name = "pg_arrow_unbounded_numeric"
version = 1

[[nodes]]
key = "src"
tool = "pg_stream_out"

[nodes.args]
connection = "pg"
sql = "select pg_sleep(30), 1::numeric as n"
wire = "arrow"
pipe_bytes = 4096
out = "rows"

[[nodes]]
key = "dst"
tool = "pg_stream_in"

[nodes.args]
connection = "pg"
schema_name = "$pg_schema"
table_name = "dst"
schema_strategy = { kind = "create_if_not_exists" }
delete_strategy = { kind = "nothing" }
insert_strategy = { kind = "full" }
feed = "rows"
"""

WIDE_NUMERIC = """
name = "pg_arrow_wide_numeric"
version = 1

[[nodes]]
key = "src"
tool = "pg_stream_out"

[nodes.args]
connection = "pg"
sql = "select pg_sleep(30), 1::numeric(50, 20) as n"
wire = "arrow"
pipe_bytes = 4096
out = "rows"

[[nodes]]
key = "dst"
tool = "pg_stream_in"

[nodes.args]
connection = "pg"
schema_name = "$pg_schema"
table_name = "dst"
schema_strategy = { kind = "create_if_not_exists" }
delete_strategy = { kind = "nothing" }
insert_strategy = { kind = "full" }
feed = "rows"
"""

BROKEN_STATEMENT = """
name = "pg_arrow_broken_statement"
version = 1

[[nodes]]
key = "src"
tool = "pg_stream_out"

[nodes.args]
connection = "pg"
sql = "select nothing from nowhere"
wire = "arrow"
pipe_bytes = 4096
out = "rows"

[[nodes]]
key = "dst"
tool = "pg_stream_in"

[nodes.args]
connection = "pg"
schema_name = "$pg_schema"
table_name = "dst"
schema_strategy = { kind = "create_if_not_exists" }
delete_strategy = { kind = "nothing" }
insert_strategy = { kind = "full" }
feed = "rows"
"""

ARRAYS_AS_TEXT = """
name = "pg_arrow_arrays_as_text"
version = 1

[[nodes]]
key = "src"
tool = "pg_stream_out"

[nodes.args]
connection = "pg"
sql = "select 1::bigint as id, array[array[1, 2], array[3, 4]] as a2"
wire = "arrow"
pipe_bytes = 4096
out = "rows"

[[nodes]]
key = "dst"
tool = "pg_stream_in"

[nodes.args]
connection = "pg"
schema_name = "$pg_schema"
table_name = "dims"
schema_strategy = { kind = "create_if_not_exists" }
delete_strategy = { kind = "nothing" }
insert_strategy = { kind = "full" }
feed = "rows"
"""

WIDE_ROW_SMALL_CHUNKS = """
name = "pg_arrow_wide_row_small_chunks"
version = 1

[[nodes]]
key = "src"
tool = "pg_stream_out"

[nodes.args]
connection = "pg"
sql = "select 1::bigint as id, repeat('x', 3 * 1024 * 1024) as t"
wire = "arrow"
pipe_bytes = 4096
out = "rows"

[[nodes]]
key = "dst"
tool = "pg_stream_in"

[nodes.args]
connection = "pg"
schema_name = "$pg_schema"
table_name = "wide"
schema_strategy = { kind = "create_if_not_exists" }
delete_strategy = { kind = "nothing" }
insert_strategy = { kind = "full" }
feed = "rows"
"""

WIDE_ROW_BIG_CHUNKS = """
name = "pg_arrow_wide_row_big_chunks"
version = 1

[[nodes]]
key = "src"
tool = "pg_stream_out"

[nodes.args]
connection = "pg"
sql = "select 1::bigint as id, repeat('x', 3 * 1024 * 1024) as t"
wire = "arrow"
max_row_bytes = 4194304
out = "rows"

[[nodes]]
key = "dst"
tool = "pg_stream_in"

[nodes.args]
connection = "pg"
schema_name = "$pg_schema"
table_name = "wide"
schema_strategy = { kind = "create_if_not_exists" }
delete_strategy = { kind = "nothing" }
insert_strategy = { kind = "full" }
feed = "rows"
"""

LIST_IN_THE_STREAM = """
name = "pg_arrow_list_in_the_stream"
version = 1

[[nodes]]
key = "src"
tool = "ch_stream_out"

[nodes.args]
connection = "ch"
sql = "select toInt64(1) as id, [toInt64(1), 2] as arr"
wire = "arrow"
pipe_bytes = 4096
out = "rows"

[[nodes]]
key = "dst"
tool = "pg_stream_in"

[nodes.args]
connection = "pg"
schema_name = "$pg_schema"
table_name = "lists"
schema_strategy = { kind = "create_if_not_exists" }
delete_strategy = { kind = "nothing" }
insert_strategy = { kind = "full" }
feed = "rows"
"""

COPY_SESSION = """
name = "pg_arrow_copy_session"
version = 1

[[nodes]]
key = "src"
tool = "pg_stream_out"

[nodes.args]
connection = "pg"
sql = '''$select'''
wire = "arrow"
pipe_bytes = 4096
out = "rows"

[[nodes]]
key = "dst"
tool = "pg_stream_in"

[nodes.args]
connection = "pg"
schema_name = "$pg_schema"
table_name = "session"
schema_strategy = { kind = "create_if_not_exists" }
delete_strategy = { kind = "nothing" }
insert_strategy = { kind = "full" }
feed = "rows"
"""

FLOATS = """
name = "pg_arrow_floats"
version = 1

[[nodes]]
key = "src"
tool = "pg_stream_out"

[nodes.args]
connection = "pg"
sql = "select g::bigint as id, g::float8 / 7 as r8 from generate_series(1, 1000) g"
wire = "arrow"
pipe_bytes = 4096
out = "rows"

[[nodes]]
key = "dst"
tool = "pg_stream_in"

[nodes.args]
connection = "pg"
schema_name = "$pg_schema"
table_name = "floats"
schema_strategy = { kind = "create_if_not_exists" }
delete_strategy = { kind = "nothing" }
insert_strategy = { kind = "full" }
feed = "rows"
"""


@dataclass(frozen=True)
class PgColumn:
    """Колонка postgres: тип, заполнение над g, минимальная версия сервера,
    сверка, тип в Arrow (как печатает pyarrow) и стороны приёмников."""

    name: str
    pg_type: str
    fill: str
    compare: Values
    arrow: str
    pg: Target
    ch: Target | None
    ora: Target | None
    nullable: bool = False
    min_version: int = 0
    unicode: bool = False

    def filled(self) -> str:
        if not self.nullable:
            return f"({self.fill})"

        return f"case when g % {NULL_EVERY} = 0 then null else ({self.fill}) end"


NFREE = "nfree::numeric(30,12)"
TZ_UTC = "tz at time zone 'UTC'"

PG_COLUMNS = (
    PgColumn(
        "id",
        "bigint",
        "g",
        NUMBER,
        "int64",
        Target("bigint"),
        Target("Int64"),
        Target("number(19)"),
    ),
    PgColumn(
        "i2",
        "smallint",
        "((g % 30000) - 15000)::smallint",
        NUMBER,
        "int16",
        Target("smallint"),
        Target("Int16"),
        Target("number(5)"),
        nullable=True,
    ),
    PgColumn(
        "i4",
        "integer",
        "((g % 3000) * 700000 - 2147483648)::int",
        NUMBER,
        "int32",
        Target("integer"),
        Target("Int32"),
        Target("number(10)"),
    ),
    PgColumn(
        "n18",
        "numeric(18,6)",
        "g::numeric / 7 - 1000",
        NUMBER,
        "decimal128(18, 6)",
        Target("numeric(18,6)"),
        Target("Decimal(18, 6)"),
        Target("number(18,6)"),
        nullable=True,
    ),
    PgColumn(
        "n38",
        "numeric(38,10)",
        "g::numeric * 1234567890.123456789 - 9999999999999999999999999.9876543210",
        NUMBER,
        "decimal128(38, 10)",
        Target("numeric(38,10)"),
        Target("Decimal(38, 10)"),
        Target("number(38,10)"),
    ),
    PgColumn(
        "nfree",
        "numeric",
        "g::numeric / 7",
        NUMBER,
        "decimal128(30, 12)",
        Target("numeric", out=NFREE, src_ref=NFREE),
        Target("Decimal(30, 12)", out=NFREE, src_ref=NFREE),
        Target("number(30,12)", out=NFREE, src_ref=NFREE),
        nullable=True,
    ),
    PgColumn(
        "r4",
        "real",
        "case g % 5 when 0 then 'NaN'::real when 1 then 'Infinity' "
        "when 2 then '-Infinity' else (g::real / 3) end",
        FLOAT32,
        "float",
        Target("real"),
        Target("Float32"),
        Target("binary_float"),
    ),
    PgColumn(
        "r8",
        "double precision",
        "case g % 5 when 0 then 'NaN'::float8 when 1 then 'Infinity' "
        "when 2 then '-0' else g::float8 / 7 end",
        FLOAT,
        "double",
        Target("double precision"),
        Target("Float64"),
        Target("binary_double"),
        nullable=True,
    ),
    PgColumn(
        "b",
        "boolean",
        "g % 2 = 0",
        NUMBER,
        "bool",
        Target("boolean"),
        Target("Bool"),
        Target("number(1)", out="b::int"),
        nullable=True,
    ),
    PgColumn(
        "t",
        "text",
        "'tab\\t' || g || E'\\nnew\\\\line ''q'' \"dq\", ко 中文 🙂'",
        EXACT,
        "large_string",
        Target("text"),
        Target("String"),
        Target("nvarchar2(400)"),
        nullable=True,
        unicode=True,
    ),
    PgColumn(
        "c5",
        "char(5)",
        "'ab'",
        EXACT,
        "large_string",
        Target("char(5)"),
        Target("String"),
        Target("char(5)"),
    ),
    PgColumn(
        "bin",
        "bytea",
        "decode(md5(g::text), 'hex') || E'\\\\000\\\\377'::bytea",
        BYTES,
        "large_string",
        Target("bytea"),
        Target("String", src_ref="bin::text"),
        Target("varchar2(200)", out="encode(bin, 'hex')", src_ref="encode(bin, 'hex')"),
        nullable=True,
    ),
    PgColumn(
        "d",
        "date",
        "date '1900-01-01' + (g % 140000)",
        DATETIME,
        "date32[day]",
        Target("date"),
        Target("Date32"),
        Target("date"),
    ),
    PgColumn(
        "ts",
        "timestamp(6)",
        "timestamp '2000-01-01 00:00:00.123456' "
        "+ g * interval '1 hour 1 minute 1.000001 second'",
        DATETIME,
        "timestamp[us]",
        Target("timestamp(6)"),
        Target("DateTime64(6, 'UTC')"),
        Target("timestamp(6)"),
        nullable=True,
    ),
    PgColumn(
        "tz",
        "timestamptz(6)",
        "timestamptz '2000-01-01 00:00:00.5+03' + g * interval '1 day'",
        DATETIME,
        "timestamp[us, tz=UTC]",
        Target("timestamptz(6)"),
        Target("DateTime64(6, 'UTC')"),
        Target("timestamp(6)", out=TZ_UTC, src_ref=TZ_UTC),
    ),
    PgColumn(
        "tm",
        "time",
        "time '00:00:00' + g * interval '1 second'",
        EXACT,
        "large_string",
        Target("time"),
        Target("String", out="tm::text", src_ref="tm::text"),
        Target("varchar2(20)", out="tm::text", src_ref="tm::text"),
    ),
    PgColumn(
        "iv",
        "interval",
        "g * interval '1 day 2 hours 3 minutes'",
        EXACT,
        "large_string",
        Target("interval", src_ref="iv::text"),
        Target("String", src_ref="iv::text"),
        Target("varchar2(40)", src_ref="iv::text"),
        nullable=True,
    ),
    PgColumn(
        "u",
        "uuid",
        "md5(g::text)::uuid",
        EXACT,
        "large_string",
        Target("uuid", src_ref="u::text"),
        Target("UUID", ref="toString(u)", src_ref="u::text"),
        Target("varchar2(36)", src_ref="u::text"),
        nullable=True,
    ),
    PgColumn(
        "ai",
        "int[]",
        "array[g, g + 1, null]",
        EXACT,
        "large_string",
        Target("int[]"),
        Target("String", src_ref="ai::text"),
        Target("varchar2(100)", src_ref="ai::text"),
    ),
    PgColumn(
        "at",
        "text[]",
        "array['a,b', 'c\"d', '{e}']",
        EXACT,
        "large_string",
        Target("text[]"),
        Target("String", src_ref="at::text"),
        Target("varchar2(100)", src_ref="at::text"),
    ),
    PgColumn(
        "an",
        "numeric(18,4)[]",
        "array[g::numeric / 3, 1.5]",
        EXACT,
        "large_string",
        Target("numeric(18,4)[]"),
        Target("String", src_ref="an::text"),
        Target("varchar2(100)", src_ref="an::text"),
    ),
    PgColumn(
        "js",
        "jsonb",
        '(\'{"a": \' || g || \', "b": {"c": null}}\')::jsonb',
        JSON,
        "large_string",
        Target("jsonb", src_ref="js::text"),
        Target("String", src_ref="js::text"),
        Target("clob check (js is json)", src_ref="js::text"),
        nullable=True,
        min_version=90400,
    ),
    PgColumn(
        "ip",
        "inet",
        "('10.0.' || (g % 256) || '.1')::inet",
        EXACT,
        "large_string",
        Target("inet", src_ref="host(ip)"),
        Target("IPv4", ref="toString(ip)", src_ref="host(ip)"),
        Target("varchar2(20)", src_ref="host(ip)"),
    ),
    PgColumn(
        "mood",
        "mood",
        "(array['sad', 'ok', 'happy'])[g % 3 + 1]::mood",
        EXACT,
        "large_string",
        Target("mood", src_ref="mood::text"),
        Target("String", src_ref="mood::text"),
        Target("varchar2(10)", src_ref="mood::text"),
        nullable=True,
    ),
    PgColumn(
        "comp",
        "pair",
        "row(g, 'x,y')::pair",
        EXACT,
        "large_string",
        Target("pair", src_ref="comp::text"),
        Target("String", src_ref="comp::text"),
        Target("varchar2(40)", src_ref="comp::text"),
    ),
    PgColumn(
        "r4r",
        "int4range",
        "int4range(g, g + 10)",
        EXACT,
        "large_string",
        Target("int4range", src_ref="r4r::text"),
        Target("String", src_ref="r4r::text"),
        Target("varchar2(40)", src_ref="r4r::text"),
        min_version=90200,
    ),
    PgColumn(
        "mo",
        "money",
        "((g::numeric / 100)::text)::money",
        EXACT,
        "large_string",
        Target("money", src_ref="mo::text"),
        Target("String", src_ref="mo::text"),
        Target("varchar2(40)", src_ref="mo::text"),
    ),
    PgColumn(
        "bt",
        "bit(4)",
        "B'1010'",
        EXACT,
        "large_string",
        Target("bit(4)", src_ref="bt::text"),
        Target("String", src_ref="bt::text"),
        Target("varchar2(4)", src_ref="bt::text"),
    ),
    PgColumn(
        "pt",
        "point",
        "point(g, g / 2.0)",
        EXACT,
        "large_string",
        Target("point", src_ref="pt::text"),
        Target("String", src_ref="pt::text"),
        Target("varchar2(40)", src_ref="pt::text"),
        nullable=True,
    ),
    PgColumn(
        "xm",
        "xml",
        "('<r id=\"' || g || '\"><v>x&amp;y</v></r>')::xml",
        EXACT,
        "large_string",
        Target("xml", src_ref="xm::text"),
        Target("String", src_ref="xm::text"),
        Target("varchar2(200)", src_ref="xm::text"),
    ),
)


class Source:
    """Таблицы src и dst в схеме postgres под версию сервера."""

    def __init__(self, side: PostgresSide) -> None:
        self.side = side

    def columns(self) -> list[PgColumn]:
        chosen: list[PgColumn] = []
        for column in PG_COLUMNS:
            if column.min_version <= self.side.version:
                chosen.append(column)

        return chosen

    async def fill(self, rows: int) -> None:
        columns = self.columns()
        ddl = ", ".join(f"{c.name} {c.pg_type}" for c in columns)
        filled = ", ".join(f"{c.filled()} as {c.name}" for c in columns)
        await self.side.recreate_schema(
            (
                f"create type {PG_SCHEMA}.mood as enum ('sad', 'ok', 'happy')",
                f"create type {PG_SCHEMA}.pair as (a int, b text)",
                f"create table {PG_SCHEMA}.src ({ddl})",
                f"create table {PG_SCHEMA}.dst ({ddl})",
                f"insert into {PG_SCHEMA}.src select {filled} "
                f"from generate_series(1, {rows}) g",
            )
        )


def _table_name(source_name: str) -> str:
    return "from_" + source_name.replace("-", "_").replace(".", "_")


def _names(columns: Sequence[PgColumn]) -> list[str]:
    return [c.name for c in columns]


def _select(columns: Sequence[PgColumn], targets: Sequence[Target]) -> str:
    listed = exported(_names(columns), targets, False)

    return f"select {listed} from {PG_SCHEMA}.src order by id"


async def _report(
    source: Source, columns: Sequence[PgColumn], targets: Sequence[Target], landed: Any
) -> Any:
    expected = await source.side.select(
        "src", [first(t.src_ref, c.name) for c, t in zip(columns, targets, strict=True)]
    )

    return compared(
        _names(columns),
        [c.compare for c in columns],
        [t.approx for t in targets],
        expected,
        landed,
    )


def _dags(
    tmp_path: Path, connections: Mapping[str, object], names: Mapping[str, str]
) -> PumpDags:
    """Платформа запуска с именами стенда и подстановками теста (select, table)."""
    substitutions: dict[str, str] = {
        "pg_schema": PG_SCHEMA,
        "ch_database": CH_DATABASE,
        "ora_schema": PumpUser().name,
    }
    substitutions.update(names)

    return PumpDags(tmp_path, connections, substitutions)


async def _landed(dags: PumpDags, spec: str) -> str:
    """Запуск описания; отчёт приёмника — узла dst."""
    outcome = await dags.run(spec)
    report = outcome.node("dst").content
    print(f"\n--- {outcome.dag} ---\n{report}")

    failed: list[str] = []
    for node in outcome.failures():
        failed.append(f"{node.key}: {node.content}")

    assert outcome.ok(), "\n".join(failed)

    return report


@pytest.fixture(scope="module", params=STAND.sources, ids=lambda s: s.name)
async def postgres(request: Any) -> AsyncIterator[PostgresSide]:
    side = PostgresSide(request.param, PG_SCHEMA)
    await side.connect()
    await Source(side).fill(ROWS)
    yield side
    await side.drop()


@pytest.fixture(scope="module", params=STAND.demo_clickhouse(), ids=lambda s: s.name)
async def clickhouse(request: Any) -> AsyncIterator[ClickHouseSide]:
    side = ClickHouseSide(request.param, CH_DATABASE)
    await side.connect()
    await side.recreate_database()
    yield side
    await side.drop()


@pytest.fixture(scope="module", params=STAND.ora_sources, ids=lambda s: s.name)
async def oracle(request: Any) -> AsyncIterator[OracleSide]:
    side = OracleSide(request.param, ARRAYSIZE)
    await side.connect()
    await side.recreate_user()
    yield side
    await side.drop()


class TestPostgresToPostgres:
    async def test_every_type_survives_the_circle(
        self, tmp_path: Path, postgres: PostgresSide
    ) -> None:
        source = Source(postgres)
        columns = source.columns()
        targets = [c.pg for c in columns]
        dags = _dags(
            tmp_path, {"pg": postgres.profile}, {"select": _select(columns, targets)}
        )

        report = await _landed(dags, CIRCLE)

        assert report.startswith(f"{ROWS} rows loaded")

        # обе стороны postgres: опорное выражение источника годится и приёмнику
        refs = [first(t.src_ref, c.name) for c, t in zip(columns, targets, strict=True)]
        landed = await postgres.select("dst", refs)
        compared_rows = await _report(source, columns, targets, landed)

        assert not compared_rows.mismatches, compared_rows.render()


class TestPostgresToClickHouse:
    async def test_every_type_lands(
        self, tmp_path: Path, postgres: PostgresSide, clickhouse: ClickHouseSide
    ) -> None:
        source = Source(postgres)
        columns: list[PgColumn] = []
        targets: list[Target] = []
        for column in source.columns():
            if column.ch is None:
                continue

            columns.append(column)
            targets.append(column.ch)

        parts: list[str] = []
        for column, target in zip(columns, targets, strict=True):
            ch_type = target.type
            if column.nullable:
                ch_type = f"Nullable({ch_type})"

            parts.append(f"{column.name} {ch_type}")

        table = _table_name(postgres.source.name)
        await clickhouse.create(table, parts)
        dags = _dags(
            tmp_path,
            {"pg": postgres.profile, "ch": clickhouse.profile},
            {"select": _select(columns, targets), "table": table},
        )

        report = await _landed(dags, TO_CLICKHOUSE)

        assert report.startswith(f"{ROWS} rows loaded")

        landed = await clickhouse.select(
            table, [first(t.ref, c.name) for c, t in zip(columns, targets, strict=True)]
        )
        compared_rows = await _report(source, columns, targets, landed)

        assert not compared_rows.mismatches, compared_rows.render()


class TestPostgresToOracle:
    LOB: ClassVar[str] = "clob"

    async def test_every_type_lands(
        self, tmp_path: Path, postgres: PostgresSide, oracle: OracleSide
    ) -> None:
        """LOB-колонки приёмника идут последними: Oracle не принимает длинный
        bind после LOB в одном insert (ORA-24816), а порядок bind'ов — порядок
        полей схемы. Текст вне ASCII в базу с однобайтовой кодировкой не
        доезжает даже в nvarchar2: bind проходит через кодировку базы."""
        source = Source(postgres)
        columns: list[PgColumn] = []
        lobs: list[PgColumn] = []
        for column in source.columns():
            if column.ora is None:
                continue

            if column.unicode and not oracle.unicode:
                continue

            if self.LOB in column.ora.type:
                lobs.append(column)
                continue

            columns.append(column)

        columns.extend(lobs)
        targets: list[Target] = []
        for column in columns:
            if column.ora is not None:
                targets.append(column.ora)

        table = _table_name(postgres.source.name)
        await oracle.create(
            table, [f"{c.name} {t.type}" for c, t in zip(columns, targets, strict=True)]
        )
        dags = _dags(
            tmp_path,
            {"pg": postgres.profile, "ora": oracle.profile},
            {"select": _select(columns, targets), "table": table},
        )
        try:
            report = await _landed(dags, TO_ORACLE)

            assert report.startswith(f"{ROWS} rows loaded")

            landed = await oracle.select(
                table,
                [first(t.ref, c.name) for c, t in zip(columns, targets, strict=True)],
            )
        finally:
            await oracle.drop_table(table)

        compared_rows = await _report(source, columns, targets, landed)

        assert not compared_rows.mismatches, compared_rows.render()


class TestTraps:
    """Ловушки Arrow-пути postgres на новейшем сервере."""

    @ON_NEWEST
    async def test_unbounded_numeric_is_refused_before_execution(
        self, tmp_path: Path, postgres: PostgresSide
    ) -> None:
        """numeric без точности отвергается по описанию стейтмента: запрос с
        pg_sleep(30) не выполняется, ответ приходит сразу."""
        dags = _dags(tmp_path, {"pg": postgres.profile}, {})
        started = time.monotonic()
        outcome = await dags.run(UNBOUNDED_NUMERIC)

        assert time.monotonic() - started < 5

        failure = outcome.failure("src")
        assert failure.error_kind == "PgArrowError", failure.llm_view()
        assert "numeric without precision" in failure.llm_view()

    @ON_NEWEST
    async def test_wide_numeric_needs_text(
        self, tmp_path: Path, postgres: PostgresSide
    ) -> None:
        """numeric шире 38 знаков читатель CSV Arrow не собирает: отказ до
        выполнения с подсказкой ::text."""
        dags = _dags(tmp_path, {"pg": postgres.profile}, {})
        outcome = await dags.run(WIDE_NUMERIC)

        failure = outcome.failure("src")
        assert failure.error_kind == "PgArrowError", failure.llm_view()
        assert "up to 38 digits" in failure.llm_view()

    @ON_NEWEST
    async def test_broken_statement_is_refused_by_describe(
        self, tmp_path: Path, postgres: PostgresSide
    ) -> None:
        dags = _dags(tmp_path, {"pg": postgres.profile}, {})
        outcome = await dags.run(BROKEN_STATEMENT)

        failure = outcome.failure("src")
        assert failure.error_kind == "PgDescribeError", failure.llm_view()
        assert "the statement on postgres failed" in failure.llm_view()

    @ON_NEWEST
    async def test_arrays_travel_as_text(
        self, tmp_path: Path, postgres: PostgresSide
    ) -> None:
        """Массив любой размерности едет текстом postgres и ложится в колонку
        массива как есть."""
        await postgres.create("dims", ["id bigint", "a2 int[][]"])
        dags = _dags(tmp_path, {"pg": postgres.profile}, {})

        await _landed(dags, ARRAYS_AS_TEXT)
        landed = await postgres.select("dims", ["id", "a2"])

        assert landed == [(1, [[1, 2], [3, 4]])]

    @ON_NEWEST
    async def test_row_wider_than_the_parse_block_needs_bigger_chunks(
        self, tmp_path: Path, postgres: PostgresSide
    ) -> None:
        """Строка CSV обязана уместиться в один блок читателя: по умолчанию
        блок — 1 MiB, строка в 3 MiB отвергается с подсказкой, а max_row_bytes
        в 4 MiB её проносит."""
        await postgres.create("wide", ["id bigint", "t text"])
        dags = _dags(tmp_path, {"pg": postgres.profile}, {})
        outcome = await dags.run(WIDE_ROW_SMALL_CHUNKS)

        failure = outcome.failure("src")
        assert failure.error_kind == "PgArrowError", failure.llm_view()
        assert "raise max_row_bytes" in failure.llm_view()

        await _landed(dags, WIDE_ROW_BIG_CHUNKS)
        landed = await postgres.select("wide", ["id", "length(t)"])

        assert landed == [(1, 3 * 1024 * 1024)]

    async def test_exact_floats_land_bit_for_bit(self, postgres: PostgresSide) -> None:
        """С exact_floats double едет hex-записью и ложится бит в бит на любом
        сервере; десятичную запись 1.942e-297 Greenplum 6 округляет на одну
        ULP — это и есть случай, ради которого флаг существует. Максимум
        double в списке нет: его сегменты Greenplum 6 отвергают как overflow
        в любой записи."""
        values = [
            1.942e-297,
            5e-324,
            2.2250738585072014e-308,
            1e308,
            -0.0,
            0.1,
            1.0 / 3,
        ]
        batch = pyarrow.record_batch(
            {
                "id": pyarrow.array(range(len(values)), pyarrow.int64()),
                "d": pyarrow.array(values, pyarrow.float64()),
            }
        )
        buffer = io.BytesIO()
        with pyarrow.ipc.new_stream(buffer, batch.schema) as writer:
            writer.write(batch)

        await postgres.create("exact", ["id bigint", "d double precision"])
        pumps = Pumps(postgres=postgres.profile)
        packed = [struct.pack(">d", value) for value in values]
        contract = ArrowContract(
            columns=StreamContract().columns(ArrowColumns().specs(batch.schema))
        )
        head = SchemaHead(
            kind="schema",
            source_engine=Engine.POSTGRES,
            wire=StreamWire.ARROW,
            contract=contract.model_dump(mode="json"),
        )

        async def land(exact_floats: bool) -> None:
            await pumps.sync_in(
                Feed(buffer.getvalue(), CHUNK_BYTES, head),
                schema_name=PG_SCHEMA,
                table_name="exact",
                copy_options=CopyOptions(exact_floats=exact_floats),
            )

        await land(True)
        landed = await postgres.select("exact", ["id", "float8send(d)"])

        assert [bytes(sent) for _, sent in landed] == packed

        if not postgres.greenplum_6:
            return

        await postgres.execute([f"truncate {PG_SCHEMA}.exact"])
        await land(False)
        landed = await postgres.select("exact", ["id", "float8send(d)"])

        assert bytes(landed[0][1]) != packed[0]

    @ON_NEWEST
    async def test_list_in_the_stream_is_refused_before_loading(
        self, tmp_path: Path, postgres: PostgresSide, clickhouse: ClickHouseSide
    ) -> None:
        """Список Arrow (Array ClickHouse) CSV не несёт: pg_stream_in отвергает
        его по схеме, источник обязан отдать текст postgres."""
        await postgres.create("lists", ["id bigint", "arr bigint[]"])
        dags = _dags(tmp_path, {"pg": postgres.profile, "ch": clickhouse.profile}, {})
        outcome = await dags.run(LIST_IN_THE_STREAM)

        failure = outcome.failure("dst")
        assert failure.error_kind == "PgArrowError", failure.llm_view()
        assert "cannot be written as csv" in failure.llm_view()

    @ON_NEWEST
    async def test_copy_session_is_fixed_regardless_of_profile_options(
        self, tmp_path: Path, postgres: PostgresSide
    ) -> None:
        """Текст COPY не зависит от настроек сессии профиля: даты ISO, UTC, bytea
        hex, интервал в записи postgres, money без локали, float точно — у
        pg_stream_out с любым wire, в том числе arrow."""
        odd = postgres.profile.options.model_copy(
            update={
                "datestyle": "Postgres, DMY",
                "intervalstyle": "sql_standard",
                "timezone": "Europe/Moscow",
                "bytea_output": "escape",
                "lc_monetary": "ru_RU.UTF-8",
                "extra_float_digits": "0",
            }
        )
        profile = postgres.profile.model_copy(update={"options": odd})
        select = (
            "select 1::bigint as id, 1.0::float8 / 3 as f, "
            "timestamptz '2024-02-29 13:14:15.123456+03' as tz, "
            "date '2024-02-29' as d, interval '1 day 2 hours 3.5 seconds' as iv, "
            "'\\x00ff'::bytea as b, 12.5::money as m"
        )
        pumps = Pumps(postgres=profile)

        text = await pumps.pg_out(select)
        await postgres.create(
            "session",
            [
                "id bigint",
                "f double precision",
                "tz timestamptz",
                "d date",
                "iv text",
                "b bytea",
                "m text",
            ],
        )
        dags = _dags(tmp_path, {"pg": profile}, {"select": select})

        await _landed(dags, COPY_SESSION)
        landed = await postgres.select(
            "session",
            ["f", "tz at time zone 'UTC'", "d", "iv", "encode(b, 'hex')", "m"],
        )

        assert text == (
            b"1\t0.3333333333333333\t2024-02-29 10:14:15.123456+00\t2024-02-29\t"
            b"1 day 02:00:03.5\t\\\\x00ff\t$12.50\n"
        )
        assert landed[0][1:] == (
            datetime(2024, 2, 29, 10, 14, 15, 123456),
            date(2024, 2, 29),
            "1 day 02:00:03.5",
            "00ff",
            "$12.50",
        )
        assert landed[0][0] == 1.0 / 3

    async def test_float_is_exact_on_every_server(
        self, tmp_path: Path, postgres: PostgresSide
    ) -> None:
        """extra_float_digits = 3 в опциях сессии выгрузки: double едет точно
        и на серверах до 12-й версии, где обычная печать даёт 15 знаков."""
        await postgres.create("floats", ["id bigint", "r8 double precision"])
        dags = _dags(tmp_path, {"pg": postgres.profile}, {})

        await _landed(dags, FLOATS)
        landed = await postgres.select("floats", ["id", "r8"])

        assert [row[1] for row in landed] == [g / 7 for g in range(1, 1001)]
