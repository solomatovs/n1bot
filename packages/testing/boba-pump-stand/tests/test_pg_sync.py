# ruff: noqa: S608, E501
"""Приёмник pg_stream_in: стратегии схемы, удаления и вставки на потоке
Arrow из postgres (круг), из Oracle и из ClickHouse на новейшем postgres;
стратегии схемы — на каждой версии postgres и Greenplum стенда.

Каждый запуск описан toml-текстом (DagSpec) и исполняется DagRunner без
модели: узлы — вызовы насосов с аргументами, как их присылает модель.
Подстановкой в описания идут имена стенда ($pg_schema, $ora_user) и
параметры матрицы: провод $wire и запись float $exact_floats (Greenplum 6).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from boba.pump_stand import Loaded, OracleSide, PostgresSide, PumpDags, PumpStand
from boba.pump_stand.oracle import PumpUser
from boba.stand.collection import StandServers
from boba.stand.names import StandNames
from boba.toolkit.transfer import StreamWire
from boba.toolrun.dag_run import DagOutcome

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

STAND = PumpStand.required()
ROWS = 60
PG_SCHEMA = StandNames().of("pump_sync")
ARRAYSIZE = 500

FRESH = """
name = "pg_sync_fresh"
version = 1

[[nodes]]
key = "out"
tool = "pg_stream_out"

[nodes.args]
connection = "pg"
sql = "select g::bigint as id, (g / 7.0)::numeric(18,4) as amount, ('name ' || g)::varchar(50) as name, timestamp '2024-02-29 13:14:15.123456' + g * interval '1 second' as ts, g % 2 = 0 as flag, g::float8 / 3 as d from generate_series(1, 60) g"
wire = "$wire"
columns = []
pipe_bytes = 4096
out = "rows"

[[nodes]]
key = "in"
tool = "pg_stream_in"

[nodes.args]
connection = "pg"
schema_name = "$pg_schema"
table_name = "fresh"
schema_strategy = { kind = "create_if_not_exists" }
delete_strategy = { kind = "nothing" }
insert_strategy = { kind = "full" }
rules = {}
copy_options = { exact_floats = $exact_floats }
feed = "rows"
"""
"""Поток-источник у всех описаний круга postgres -> postgres один: шесть
семейств типов, 60 строк."""

TWICE_CREATE = """
name = "pg_sync_twice_create"
version = 1

[[nodes]]
key = "out"
tool = "pg_stream_out"

[nodes.args]
connection = "pg"
sql = "select g::bigint as id, (g / 7.0)::numeric(18,4) as amount, ('name ' || g)::varchar(50) as name, timestamp '2024-02-29 13:14:15.123456' + g * interval '1 second' as ts, g % 2 = 0 as flag, g::float8 / 3 as d from generate_series(1, 60) g"
wire = "$wire"
columns = []
pipe_bytes = 4096
out = "rows"

[[nodes]]
key = "in"
tool = "pg_stream_in"

[nodes.args]
connection = "pg"
schema_name = "$pg_schema"
table_name = "twice"
schema_strategy = { kind = "create_if_not_exists" }
delete_strategy = { kind = "nothing" }
insert_strategy = { kind = "full" }
rules = {}
copy_options = { exact_floats = $exact_floats }
feed = "rows"
"""

TWICE_KEPT = """
name = "pg_sync_twice_kept"
version = 1

[[nodes]]
key = "out"
tool = "pg_stream_out"

[nodes.args]
connection = "pg"
sql = "select g::bigint as id, (g / 7.0)::numeric(18,4) as amount, ('name ' || g)::varchar(50) as name, timestamp '2024-02-29 13:14:15.123456' + g * interval '1 second' as ts, g % 2 = 0 as flag, g::float8 / 3 as d from generate_series(1, 60) g"
wire = "$wire"
columns = []
pipe_bytes = 4096
out = "rows"

[[nodes]]
key = "in"
tool = "pg_stream_in"

[nodes.args]
connection = "pg"
schema_name = "$pg_schema"
table_name = "twice"
schema_strategy = { kind = "error_if_schema_changed" }
delete_strategy = { kind = "nothing" }
insert_strategy = { kind = "full" }
rules = {}
copy_options = { exact_floats = $exact_floats }
feed = "rows"
"""

ABSENT = """
name = "pg_sync_absent"
version = 1

[[nodes]]
key = "out"
tool = "pg_stream_out"

[nodes.args]
connection = "pg"
sql = "select g::bigint as id, (g / 7.0)::numeric(18,4) as amount, ('name ' || g)::varchar(50) as name, timestamp '2024-02-29 13:14:15.123456' + g * interval '1 second' as ts, g % 2 = 0 as flag, g::float8 / 3 as d from generate_series(1, 60) g"
wire = "$wire"
columns = []
pipe_bytes = 4096
out = "rows"

[[nodes]]
key = "in"
tool = "pg_stream_in"

[nodes.args]
connection = "pg"
schema_name = "$pg_schema"
table_name = "absent"
schema_strategy = { kind = "error_if_not_exists" }
delete_strategy = { kind = "nothing" }
insert_strategy = { kind = "full" }
rules = {}
copy_options = { exact_floats = $exact_floats }
feed = "rows"
"""

NARROW = """
name = "pg_sync_narrow"
version = 1

[[nodes]]
key = "out"
tool = "pg_stream_out"

[nodes.args]
connection = "pg"
sql = "select g::bigint as id, (g / 7.0)::numeric(18,4) as amount, ('name ' || g)::varchar(50) as name, timestamp '2024-02-29 13:14:15.123456' + g * interval '1 second' as ts, g % 2 = 0 as flag, g::float8 / 3 as d from generate_series(1, 60) g"
wire = "$wire"
columns = []
pipe_bytes = 4096
out = "rows"

[[nodes]]
key = "in"
tool = "pg_stream_in"

[nodes.args]
connection = "pg"
schema_name = "$pg_schema"
table_name = "narrow"
schema_strategy = { kind = "error_if_schema_changed" }
delete_strategy = { kind = "nothing" }
insert_strategy = { kind = "full" }
rules = {}
copy_options = { exact_floats = $exact_floats }
feed = "rows"
"""

KEEP_OLD = """
name = "pg_sync_keep_old"
version = 1

[[nodes]]
key = "out"
tool = "pg_stream_out"

[nodes.args]
connection = "pg"
sql = "select g::bigint as id, (g / 7.0)::numeric(18,4) as amount, ('name ' || g)::varchar(50) as name, timestamp '2024-02-29 13:14:15.123456' + g * interval '1 second' as ts, g % 2 = 0 as flag, g::float8 / 3 as d from generate_series(1, 60) g"
wire = "$wire"
columns = []
pipe_bytes = 4096
out = "rows"

[[nodes]]
key = "in"
tool = "pg_stream_in"

[nodes.args]
connection = "pg"
schema_name = "$pg_schema"
table_name = "keep_old"
schema_strategy = { kind = "backup_and_create_if_schema_changed" }
delete_strategy = { kind = "nothing" }
insert_strategy = { kind = "full" }
rules = {}
copy_options = { exact_floats = $exact_floats }
feed = "rows"
"""

REPLACED = """
name = "pg_sync_replaced"
version = 1

[[nodes]]
key = "out"
tool = "pg_stream_out"

[nodes.args]
connection = "pg"
sql = "select g::bigint as id, (g / 7.0)::numeric(18,4) as amount, ('name ' || g)::varchar(50) as name, timestamp '2024-02-29 13:14:15.123456' + g * interval '1 second' as ts, g % 2 = 0 as flag, g::float8 / 3 as d from generate_series(1, 60) g"
wire = "$wire"
columns = []
pipe_bytes = 4096
out = "rows"

[[nodes]]
key = "in"
tool = "pg_stream_in"

[nodes.args]
connection = "pg"
schema_name = "$pg_schema"
table_name = "replaced"
schema_strategy = { kind = "drop_and_create_if_schema_changed" }
delete_strategy = { kind = "nothing" }
insert_strategy = { kind = "full" }
rules = {}
copy_options = { exact_floats = $exact_floats }
feed = "rows"
"""

TRUNC_CREATE = """
name = "pg_sync_trunc_create"
version = 1

[[nodes]]
key = "out"
tool = "pg_stream_out"

[nodes.args]
connection = "pg"
sql = "select g::bigint as id, (g / 7.0)::numeric(18,4) as amount, ('name ' || g)::varchar(50) as name, timestamp '2024-02-29 13:14:15.123456' + g * interval '1 second' as ts, g % 2 = 0 as flag, g::float8 / 3 as d from generate_series(1, 60) g"
wire = "$wire"
columns = []
pipe_bytes = 4096
out = "rows"

[[nodes]]
key = "in"
tool = "pg_stream_in"

[nodes.args]
connection = "pg"
schema_name = "$pg_schema"
table_name = "trunc"
schema_strategy = { kind = "create_if_not_exists" }
delete_strategy = { kind = "nothing" }
insert_strategy = { kind = "full" }
rules = {}
copy_options = { exact_floats = $exact_floats }
feed = "rows"
"""

TRUNC_RELOAD = """
name = "pg_sync_trunc_reload"
version = 1

[[nodes]]
key = "out"
tool = "pg_stream_out"

[nodes.args]
connection = "pg"
sql = "select g::bigint as id, (g / 7.0)::numeric(18,4) as amount, ('name ' || g)::varchar(50) as name, timestamp '2024-02-29 13:14:15.123456' + g * interval '1 second' as ts, g % 2 = 0 as flag, g::float8 / 3 as d from generate_series(1, 60) g"
wire = "$wire"
columns = []
pipe_bytes = 4096
out = "rows"

[[nodes]]
key = "in"
tool = "pg_stream_in"

[nodes.args]
connection = "pg"
schema_name = "$pg_schema"
table_name = "trunc"
schema_strategy = { kind = "create_if_not_exists" }
delete_strategy = { kind = "truncate" }
insert_strategy = { kind = "full" }
rules = {}
copy_options = { exact_floats = $exact_floats }
feed = "rows"
"""

WIPE_CREATE = """
name = "pg_sync_wipe_create"
version = 1

[[nodes]]
key = "out"
tool = "pg_stream_out"

[nodes.args]
connection = "pg"
sql = "select g::bigint as id, (g / 7.0)::numeric(18,4) as amount, ('name ' || g)::varchar(50) as name, timestamp '2024-02-29 13:14:15.123456' + g * interval '1 second' as ts, g % 2 = 0 as flag, g::float8 / 3 as d from generate_series(1, 60) g"
wire = "$wire"
columns = []
pipe_bytes = 4096
out = "rows"

[[nodes]]
key = "in"
tool = "pg_stream_in"

[nodes.args]
connection = "pg"
schema_name = "$pg_schema"
table_name = "wipe"
schema_strategy = { kind = "create_if_not_exists" }
delete_strategy = { kind = "nothing" }
insert_strategy = { kind = "full" }
rules = {}
copy_options = { exact_floats = $exact_floats }
feed = "rows"
"""

WIPE_RELOAD = """
name = "pg_sync_wipe_reload"
version = 1

[[nodes]]
key = "out"
tool = "pg_stream_out"

[nodes.args]
connection = "pg"
sql = "select g::bigint as id, (g / 7.0)::numeric(18,4) as amount, ('name ' || g)::varchar(50) as name, timestamp '2024-02-29 13:14:15.123456' + g * interval '1 second' as ts, g % 2 = 0 as flag, g::float8 / 3 as d from generate_series(1, 60) g"
wire = "$wire"
columns = []
pipe_bytes = 4096
out = "rows"

[[nodes]]
key = "in"
tool = "pg_stream_in"

[nodes.args]
connection = "pg"
schema_name = "$pg_schema"
table_name = "wipe"
schema_strategy = { kind = "create_if_not_exists" }
delete_strategy = { kind = "delete_all" }
insert_strategy = { kind = "full" }
rules = {}
copy_options = { exact_floats = $exact_floats }
feed = "rows"
"""

PART_CREATE = """
name = "pg_sync_part_create"
version = 1

[[nodes]]
key = "out"
tool = "pg_stream_out"

[nodes.args]
connection = "pg"
sql = "select g::bigint as id, (g / 7.0)::numeric(18,4) as amount, ('name ' || g)::varchar(50) as name, timestamp '2024-02-29 13:14:15.123456' + g * interval '1 second' as ts, g % 2 = 0 as flag, g::float8 / 3 as d from generate_series(1, 60) g"
wire = "$wire"
columns = []
pipe_bytes = 4096
out = "rows"

[[nodes]]
key = "in"
tool = "pg_stream_in"

[nodes.args]
connection = "pg"
schema_name = "$pg_schema"
table_name = "part"
schema_strategy = { kind = "create_if_not_exists" }
delete_strategy = { kind = "nothing" }
insert_strategy = { kind = "full" }
rules = {}
copy_options = { exact_floats = $exact_floats }
feed = "rows"
"""

PART_HALF_REPLACED = """
name = "pg_sync_part_half_replaced"
version = 1

[[nodes]]
key = "out"
tool = "pg_stream_out"

[nodes.args]
connection = "pg"
sql = "select g::bigint as id, (g / 7.0)::numeric(18,4) as amount, ('name ' || g)::varchar(50) as name, timestamp '2024-02-29 13:14:15.123456' + g * interval '1 second' as ts, g % 2 = 0 as flag, g::float8 / 3 as d from generate_series(1, 60) g"
wire = "$wire"
columns = []
pipe_bytes = 4096
out = "rows"

[[nodes]]
key = "in"
tool = "pg_stream_in"

[nodes.args]
connection = "pg"
schema_name = "$pg_schema"
table_name = "part"
schema_strategy = { kind = "create_if_not_exists" }
delete_strategy = { kind = "delete_where", where = "id <= 30" }
insert_strategy = { kind = "full" }
rules = {}
copy_options = { exact_floats = $exact_floats }
feed = "rows"
"""

PART_EMPTIED = """
name = "pg_sync_part_emptied"
version = 1

[[nodes]]
key = "out"
tool = "pg_stream_out"

[nodes.args]
connection = "pg"
sql = "select g::bigint as id, (g / 7.0)::numeric(18,4) as amount, ('name ' || g)::varchar(50) as name, timestamp '2024-02-29 13:14:15.123456' + g * interval '1 second' as ts, g % 2 = 0 as flag, g::float8 / 3 as d from generate_series(1, 60) g"
wire = "$wire"
columns = []
pipe_bytes = 4096
out = "rows"

[[nodes]]
key = "in"
tool = "pg_stream_in"

[nodes.args]
connection = "pg"
schema_name = "$pg_schema"
table_name = "part"
schema_strategy = { kind = "create_if_not_exists" }
delete_strategy = { kind = "delete_where", where = "id between 1 and 60" }
insert_strategy = { kind = "nothing" }
rules = {}
copy_options = { exact_floats = $exact_floats }
feed = "rows"
"""

SHAPED = """
name = "pg_sync_shaped"
version = 1

[[nodes]]
key = "out"
tool = "pg_stream_out"

[nodes.args]
connection = "pg"
sql = "select g::bigint as id, (g / 7.0)::numeric(18,4) as amount, ('name ' || g)::varchar(50) as name, timestamp '2024-02-29 13:14:15.123456' + g * interval '1 second' as ts, g % 2 = 0 as flag, g::float8 / 3 as d from generate_series(1, 60) g"
wire = "$wire"
columns = []
pipe_bytes = 4096
out = "rows"

[[nodes]]
key = "in"
tool = "pg_stream_in"

[nodes.args]
connection = "pg"
schema_name = "$pg_schema"
table_name = "shaped"
schema_strategy = { kind = "create_if_not_exists" }
delete_strategy = { kind = "nothing" }
insert_strategy = { kind = "full" }
rules = { rename_columns = { title = "name" }, column_types = { amount = "numeric(20,6)" } }
copy_options = { exact_floats = $exact_floats }
feed = "rows"
"""

STRICT = """
name = "pg_sync_strict"
version = 1

[[nodes]]
key = "out"
tool = "pg_stream_out"

[nodes.args]
connection = "pg"
sql = "select * from $pg_schema.strict_src where id <= 50"
wire = "$wire"
columns = [{ name = "id", nullable = false }, { name = "amount", nullable = false }]
pipe_bytes = 4096
out = "rows"

[[nodes]]
key = "in"
tool = "pg_stream_in"

[nodes.args]
connection = "pg"
schema_name = "$pg_schema"
table_name = "strict_dst"
schema_strategy = { kind = "create_if_not_exists" }
delete_strategy = { kind = "nothing" }
insert_strategy = { kind = "full" }
rules = {}
copy_options = { exact_floats = $exact_floats }
feed = "rows"
"""

UNKNOWN_DECLARATION = """
name = "pg_sync_unknown_declaration"
version = 1

[[nodes]]
key = "out"
tool = "pg_stream_out"

[nodes.args]
connection = "pg"
sql = "select g::bigint as id, (g / 7.0)::numeric(18,4) as amount, ('name ' || g)::varchar(50) as name, timestamp '2024-02-29 13:14:15.123456' + g * interval '1 second' as ts, g % 2 = 0 as flag, g::float8 / 3 as d from generate_series(1, 60) g"
wire = "$wire"
columns = [{ name = "nope", nullable = false }]
pipe_bytes = 4096
out = "rows"

[[nodes]]
key = "in"
tool = "pg_stream_in"

[nodes.args]
connection = "pg"
schema_name = "$pg_schema"
table_name = "never"
schema_strategy = { kind = "create_if_not_exists" }
delete_strategy = { kind = "nothing" }
insert_strategy = { kind = "full" }
rules = {}
copy_options = { exact_floats = $exact_floats }
feed = "rows"
"""

ORA_STRICT = """
name = "pg_sync_ora_strict"
version = 1

[[nodes]]
key = "out"
tool = "ora_stream_out"

[nodes.args]
connection = "ora"
sql = "select * from $ora_user.strict where id <= 50"
columns = [{ name = "ID", nullable = false }, { name = "AMOUNT", nullable = false }]
out = "rows"

[[nodes]]
key = "in"
tool = "pg_stream_in"

[nodes.args]
connection = "pg"
schema_name = "$pg_schema"
table_name = "strict_from_ora"
schema_strategy = { kind = "create_if_not_exists" }
delete_strategy = { kind = "nothing" }
insert_strategy = { kind = "full" }
feed = "rows"
"""

ORA_TYPES = """
name = "pg_sync_ora_types"
version = 1

[[nodes]]
key = "out"
tool = "ora_stream_out"

[nodes.args]
connection = "ora"
sql = '''select id as "id", amount as "amount", name as "name", ts as "ts", '\\\\x' || rawtohex(rw) as "rw" from $ora_user.src'''
columns = []
out = "rows"

[[nodes]]
key = "in"
tool = "pg_stream_in"

[nodes.args]
connection = "pg"
schema_name = "$pg_schema"
table_name = "from_oracle"
schema_strategy = { kind = "create_if_not_exists" }
delete_strategy = { kind = "nothing" }
insert_strategy = { kind = "full" }
feed = "rows"
"""


NEWEST = STAND.newest_postgres()
ON_NEWEST = StandServers().only("postgres", [NEWEST])
"""Случай не зависит от версии сервера: идёт на одном, самом новом postgres."""


@pytest.fixture(
    scope="module",
    params=[StreamWire.ARROW, StreamWire.CSV],
    ids=["arrow", "copy"],
)
def mode(request: Any) -> StreamWire:
    """Раскладка провода pg -> pg: arrow (нейтральный путь) или csv (пара)."""
    return request.param


@pytest.fixture(scope="module", params=STAND.sources, ids=lambda s: s.name)
async def postgres(request: Any, mode: StreamWire) -> AsyncIterator[PostgresSide]:
    """Схема пересоздаётся на каждую пару (режим провода, сервер)."""
    side = PostgresSide(request.param, PG_SCHEMA)
    await side.connect()
    await side.recreate_schema()
    yield side
    await side.drop()


@pytest.fixture(scope="module")
async def oracle(mode: StreamWire) -> AsyncIterator[OracleSide]:
    """Пользователь стенда пересоздаётся на каждый режим провода."""
    side = OracleSide(max(STAND.ora_sources, key=lambda s: s.name), ARRAYSIZE)
    await side.connect()
    await side.recreate_user()
    yield side
    await side.drop()


def toml_bool(value: bool) -> str:
    if value:
        return "true"

    return "false"


@pytest.fixture
def dags(tmp_path: Path, postgres: PostgresSide, mode: StreamWire) -> PumpDags:
    """Запуск описаний postgres -> postgres на одном сервере."""
    return PumpDags(
        tmp_path,
        {"pg": postgres.profile},
        {
            "pg_schema": PG_SCHEMA,
            "wire": mode.value,
            "exact_floats": toml_bool(postgres.greenplum_6),
        },
    )


@pytest.fixture
def ora_dags(tmp_path: Path, postgres: PostgresSide, oracle: OracleSide) -> PumpDags:
    """Запуск описаний Oracle -> postgres."""
    return PumpDags(
        tmp_path,
        {"pg": postgres.profile, "ora": oracle.profile},
        {"pg_schema": PG_SCHEMA, "ora_user": PumpUser().name},
    )


def landed(outcome: DagOutcome) -> str:
    """Отчёт приёмника удавшегося запуска."""
    report = outcome.node("in").content
    print(f"\n--- {outcome.dag}: source ---\n{outcome.node('out').content}")
    print(f"--- pg_stream_in ---\n{report}")

    assert outcome.ok(), report

    return report


class TestSchemaStrategies:
    async def test_create_if_not_exists_builds_the_table_from_the_stream(
        self, dags: PumpDags, postgres: PostgresSide
    ) -> None:
        report = landed(await dags.run(FRESH))
        loaded = Loaded(postgres, PG_SCHEMA, "fresh")

        assert report.startswith(f"{ROWS} rows loaded into {PG_SCHEMA}.fresh")
        assert "table: created (table is missing)" in report
        assert await loaded.columns() == [
            ("id", "bigint", False),
            ("amount", "numeric(18,4)", False),
            ("name", "character varying(50)", False),
            ("ts", "timestamp without time zone", False),
            ("flag", "boolean", False),
            ("d", "double precision", False),
        ]
        assert await loaded.count() == ROWS

    async def test_second_run_keeps_the_table_and_appends(
        self, dags: PumpDags, postgres: PostgresSide
    ) -> None:
        landed(await dags.run(TWICE_CREATE))
        report = landed(await dags.run(TWICE_KEPT))

        assert "table: kept as is (schema matches)" in report
        assert await Loaded(postgres, PG_SCHEMA, "twice").count() == 2 * ROWS

    async def test_error_if_not_exists_refuses_a_missing_table(
        self, dags: PumpDags
    ) -> None:
        outcome = await dags.run(ABSENT)

        failure = outcome.failure("in")
        assert failure.error_kind == "TransferError"
        assert "table is missing" in failure.llm_view()

    async def test_narrowed_column_is_a_schema_change(
        self, dags: PumpDags, postgres: PostgresSide
    ) -> None:
        await postgres.create(
            "narrow",
            [
                "id bigint",
                "amount numeric(10,2)",
                "name varchar(50)",
                "ts timestamp(6)",
                "flag boolean",
                "d double precision",
            ],
        )
        outcome = await dags.run(NARROW)

        failure = outcome.failure("in")
        assert failure.error_kind == "TransferError"
        assert "amount" in failure.llm_view()

    async def test_backup_and_create_renames_the_old_table(
        self, dags: PumpDags, postgres: PostgresSide
    ) -> None:
        await postgres.create("keep_old", ["id bigint", "extra text"])
        report = landed(await dags.run(KEEP_OLD))
        loaded = Loaded(postgres, PG_SCHEMA, "keep_old")
        tables = await loaded.tables()

        assert "table: recreated (" in report
        assert "saved as" in report
        assert "keep_old_bak_" in report
        assert [t for t in tables if t.startswith("keep_old_bak_")]
        assert await loaded.count() == ROWS

    async def test_drop_and_create_replaces_the_old_table(
        self, dags: PumpDags, postgres: PostgresSide
    ) -> None:
        await postgres.create("replaced", ["id bigint", "extra text"])
        report = landed(await dags.run(REPLACED))
        loaded = Loaded(postgres, PG_SCHEMA, "replaced")

        assert "table: dropped and recreated" in report
        assert [c[0] for c in await loaded.columns()] == [
            "id",
            "amount",
            "name",
            "ts",
            "flag",
            "d",
        ]
        assert not [t for t in await loaded.tables() if t.startswith("replaced_bak_")]


@ON_NEWEST
class TestDeleteAndInsert:
    async def test_truncate_then_full(
        self, dags: PumpDags, postgres: PostgresSide
    ) -> None:
        landed(await dags.run(TRUNC_CREATE))
        report = landed(await dags.run(TRUNC_RELOAD))

        assert "removed by truncate" in report
        assert "statement: truncate table" in report
        assert await Loaded(postgres, PG_SCHEMA, "trunc").count() == ROWS

    async def test_delete_all_counts_rows(
        self, dags: PumpDags, postgres: PostgresSide
    ) -> None:
        landed(await dags.run(WIPE_CREATE))
        report = landed(await dags.run(WIPE_RELOAD))

        assert f"{ROWS} rows deleted" in report
        assert "statement: delete from" in report
        assert await Loaded(postgres, PG_SCHEMA, "wipe").count() == ROWS

    async def test_delete_where_then_nothing_inserted(
        self, dags: PumpDags, postgres: PostgresSide
    ) -> None:
        landed(await dags.run(PART_CREATE))
        half = ROWS // 2
        landed(await dags.run(PART_HALF_REPLACED))

        assert await Loaded(postgres, PG_SCHEMA, "part").count() == 2 * ROWS - half

        report = landed(await dags.run(PART_EMPTIED))

        assert f"{2 * ROWS - half} rows deleted" in report
        assert "0 rows loaded" in report
        assert await Loaded(postgres, PG_SCHEMA, "part").count() == 0

    async def test_rename_and_declaration_shape_the_ddl(
        self, dags: PumpDags, postgres: PostgresSide
    ) -> None:
        landed(await dags.run(SHAPED))
        columns = await Loaded(postgres, PG_SCHEMA, "shaped").columns()

        assert ("title", "character varying(50)", False) in columns
        assert ("amount", "numeric(20,6)", False) in columns
        assert await Loaded(postgres, PG_SCHEMA, "shaped").count() == ROWS


@ON_NEWEST
class TestDeclarations:
    """Декларации columns у источника несут not null и тип в контракт:
    приёмник создаёт таблицу с теми же ограничениями."""

    async def test_pg_declarations_carry_not_null(
        self, dags: PumpDags, postgres: PostgresSide
    ) -> None:
        await postgres.create(
            "strict_src",
            ["id bigint not null", "note text", "amount numeric(12,2) not null"],
        )
        await postgres.execute(
            [
                f"insert into {PG_SCHEMA}.strict_src "
                "select g, 'n' || g, g / 3.0 from generate_series(1, 100) g"
            ]
        )
        report = landed(await dags.run(STRICT))

        assert report.startswith("50 rows loaded")
        assert await Loaded(postgres, PG_SCHEMA, "strict_dst").columns() == [
            ("id", "bigint", True),
            ("note", "text", False),
            ("amount", "numeric(12,2)", True),
        ]

    async def test_unknown_column_in_declarations_is_refused(
        self, dags: PumpDags
    ) -> None:
        """Отказ источника: у пары COPY — TransferError, у arrow —
        ContractError, текст один."""
        outcome = await dags.run(UNKNOWN_DECLARATION)

        failure = outcome.failure("out")
        assert "has no column 'nope'" in failure.llm_view()

    async def test_ora_declarations_carry_not_null(
        self, ora_dags: PumpDags, postgres: PostgresSide, oracle: OracleSide
    ) -> None:
        await oracle.create(
            "STRICT",
            [
                "ID number(10) not null",
                "NOTE varchar2(20)",
                "AMOUNT number(12,2) not null",
            ],
        )
        await oracle.run(
            (
                f"insert into {PumpUser().name}.strict select level, 'n' || level, "
                "level / 3 from dual connect by level <= 100",
            )
        )
        report = landed(await ora_dags.run(ORA_STRICT))

        assert report.startswith("50 rows loaded")
        assert await Loaded(postgres, PG_SCHEMA, "strict_from_ora").columns() == [
            ("ID", "bigint", True),
            ("NOTE", "character varying(20)", False),
            ("AMOUNT", "numeric(12,2)", True),
        ]


@ON_NEWEST
class TestOtherSources:
    async def test_oracle_stream_creates_exact_types(
        self, ora_dags: PumpDags, postgres: PostgresSide, oracle: OracleSide
    ) -> None:
        await oracle.create(
            "src",
            [
                "id number(10)",
                "amount number(18,4)",
                "name varchar2(50)",
                "ts timestamp(6)",
                "rw raw(16)",
            ],
        )
        await oracle.run(
            (
                f"insert into {PumpUser().name}.src select level, level / 7, "
                "'n' || level, systimestamp, hextoraw('00ff') from dual "
                "connect by level <= 100",
            )
        )
        report = landed(await ora_dags.run(ORA_TYPES))
        columns = await Loaded(postgres, PG_SCHEMA, "from_oracle").columns()

        assert report.startswith("100 rows loaded")
        assert ("id", "bigint", False) in columns
        assert ("amount", "numeric(18,4)", False) in columns
        assert ("name", "character varying(50)", False) in columns
        assert ("ts", "timestamp(6) without time zone", False) in columns
        assert ("rw", "character varying(35)", False) in columns
