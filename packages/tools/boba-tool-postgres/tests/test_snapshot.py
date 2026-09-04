"""pg_schema_snapshot против живого Postgres: схема со всеми видами объектов
снимается порциями в кадры, кадры складываются в PgSnapshot, и в нём есть
всё, что было создано: таблицы с ключами и индексами, секции, view,
материализованное view, функции и процедуры с аргументами,
последовательность с владельцем, enum, domain и composite."""

from __future__ import annotations

import os
import threading
from typing import Any, ClassVar

import pytest
from psycopg import sql
from pydantic import TypeAdapter

from boba.catalog import (
    SnapshotAssembler,
    SourceKinds,
    SyncBatch,
    SyncDone,
    SyncFrame,
    SyncFrameKind,
    SyncPlan,
)
from boba.db.postgres import AsyncPostgresPool
from boba.db.postgres.profile import PostgresConfig
from boba.db.postgres.snapshot import (
    PgConstraintKind,
    PgRelationKind,
    PgRoutineKind,
    PgSnapshot,
    PgTypeKind,
)
from boba.runtime.config import RuntimeConfig
from boba.stand.database import TestDatabase
from boba.tool.pg.snapshot import pg_schema_snapshot
from boba.toolkit.entry import ToolMain
from boba.toolkit.frames import FrameCodec, FrameLimit, ToolFrame, ToolIo
from boba.toolkit.ports import Outbound

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

SCHEMA = "snap_test"
ETL = "snap_etl"


class Fixture:
    """DDL схемы под снимок: по одному объекту каждого вида."""

    STATEMENTS: ClassVar[tuple[str, ...]] = (
        "create schema {s}",
        "create schema {e}",
        "comment on schema {s} is 'snapshot fixture'",
        "create type {s}.mood as enum ('sad', 'ok', 'happy')",
        "create domain {s}.positive_int as integer check (value > 0)",
        "create type {s}.point2 as (x double precision, y double precision)",
        "create sequence {s}.orders_seq start 10 increment 5",
        """
        create table {s}.customers (
            id integer primary key,
            name text not null,
            mood {s}.mood default 'ok',
            created_at timestamptz default now(),
            note text collate "C"
        )
        """,
        "comment on table {s}.customers is 'people'",
        "comment on column {s}.customers.name is 'full name'",
        "create index customers_name_idx on {s}.customers (lower(name))",
        """
        create table {s}.orders (
            id bigint not null default nextval('{s}.orders_seq'),
            customer_id integer references {s}.customers (id)
                on delete cascade on update restrict deferrable,
            amount numeric(12, 2) check (amount >= 0),
            day date not null,
            primary key (id, day),
            unique (customer_id, day)
        ) partition by range (day)
        """,
        "alter sequence {s}.orders_seq owned by {s}.orders.id",
        """
        create table {s}.orders_2026 partition of {s}.orders
            for values from ('2026-01-01') to ('2027-01-01')
        """,
        "create index orders_day_idx on {s}.orders (day) where amount > 0",
        """
        create view {s}.v_orders as
            select o.id, o.day, c.name from {s}.orders o
            join {s}.customers c on c.id = o.customer_id
        """,
        """
        create view {s}.v_happy with (check_option = local) as
            select id, name from {s}.customers where mood = 'happy'
        """,
        """
        create materialized view {s}.mv_totals as
            select day, sum(amount) as total from {s}.orders group by day
        """,
        """
        create function {e}.load_orders(day date, batch integer default 100)
        returns integer language sql stable as $$ select batch $$
        """,
        "comment on function {e}.load_orders(date, integer) is 'loads a day'",
        """
        create procedure {e}.refresh_totals(inout touched integer)
        language plpgsql as $$ begin touched := touched + 1; end $$
        """,
        """
        create function {e}.rows_of(prefix text)
        returns setof text language sql immutable strict
        as $$ select prefix || '1' union all select prefix || '2' $$
        """,
    )
    RELATIONS: ClassVar[tuple[str, ...]] = (
        "customers",
        "orders",
        "orders_2026",
        "v_orders",
        "v_happy",
        "mv_totals",
    )
    OBJECTS: ClassVar[int] = 6 + 3 + 1 + 3
    """Отношения, рутины, последовательность, типы."""
    BATCH_SIZE: ClassVar[int] = 2

    @classmethod
    async def prepare(cls, pool: AsyncPostgresPool) -> None:
        async with pool.connection() as conn:
            for schema in (SCHEMA, ETL):
                await conn.execute(
                    sql.SQL("drop schema if exists {} cascade").format(
                        sql.Identifier(schema)
                    )
                )

            for statement in cls.STATEMENTS:
                text = statement.format(s=SCHEMA, e=ETL).encode()
                await conn.execute(text, prepare=False)


class FrameSink:
    """Читает кадры инструмента из пайпа в своём потоке, пока тело пишет."""

    READ_BYTES: ClassVar[int] = 65536

    def __init__(self) -> None:
        self.read_fd, self.write_fd = os.pipe()
        self.frames: list[ToolFrame] = []
        self._codec = FrameCodec(FrameLimit.HEADER_BYTES, FrameLimit.BODY_BYTES)
        self._thread = threading.Thread(target=self._drain, daemon=True)
        self._thread.start()

    def _drain(self) -> None:
        while True:
            chunk = os.read(self.read_fd, self.READ_BYTES)
            if not chunk:
                break

            self.frames.extend(self._codec.feed(chunk))

        self._codec.finish()

    def close(self) -> list[ToolFrame]:
        os.close(self.write_fd)
        self._thread.join(timeout=30)
        os.close(self.read_fd)
        return self.frames


FRAME_ADAPTER: TypeAdapter[SyncFrame] = TypeAdapter(SyncFrame)


def _assemble(frames: list[ToolFrame]) -> tuple[SyncPlan, SyncDone, PgSnapshot]:
    heads = [FRAME_ADAPTER.validate_json(frame.header) for frame in frames]
    plan = heads[0]
    if not isinstance(plan, SyncPlan):
        raise AssertionError(f"the first frame must be the plan, got {plan!r}")

    assembler = SnapshotAssembler(plan, SourceKinds.of(PgSnapshot))
    done: SyncDone | None = None
    for head, frame in zip(heads[1:], frames[1:], strict=True):
        if isinstance(head, SyncBatch):
            assembler.take(head, frame.body)
        elif isinstance(head, SyncDone):
            done = head
        else:
            raise AssertionError(f"unexpected frame after the plan: {head!r}")

    if done is None:
        raise AssertionError("no done frame among the tool frames")

    snapshot = assembler.finish(done)
    if not isinstance(snapshot, PgSnapshot):
        raise AssertionError(f"expected a PgSnapshot, got {type(snapshot).__name__}")

    return plan, done, snapshot


async def _run_tool(
    connection: PostgresConfig, schemas: str, batch_size: int
) -> tuple[list[ToolFrame], dict[str, Any]]:
    body = ToolMain.toolset(pg_schema_snapshot)[0].coroutine
    if body is None:
        raise AssertionError("pg_schema_snapshot has no coroutine body")

    sink = FrameSink()
    out: Outbound[SyncFrame] = Outbound(ToolIo.on_channels(-1, sink.write_fd))
    try:
        _content, artifact = await body(
            connection=connection,
            schemas=schemas,
            batch_size=batch_size,
            pause_ms=0,
            out=out,
        )
    finally:
        frames = sink.close()

    return frames, artifact.model_dump(mode="json")


@pytest.fixture
async def connection(
    runtime_config: RuntimeConfig, test_database: str, pool: AsyncPostgresPool
) -> PostgresConfig:
    await Fixture.prepare(pool)
    return TestDatabase.config_of(runtime_config.data_layer.postgres, test_database)


class Taken:
    """Один прогон инструмента на модуль: кадры, план, итог и снимок."""

    def __init__(self, frames: list[ToolFrame], artifact: dict[str, Any]) -> None:
        self.frames = frames
        self.artifact = artifact
        self.plan, self.done, self.snapshot = _assemble(frames)


@pytest.fixture
async def taken(connection: PostgresConfig) -> Taken:
    frames, artifact = await _run_tool(
        connection, f"{SCHEMA}, {ETL}", Fixture.BATCH_SIZE
    )
    return Taken(frames, artifact)


async def test_plan_batches_and_done_agree(
    taken: Taken, connection: PostgresConfig
) -> None:
    plan = taken.plan
    assert plan.kind is SyncFrameKind.PLAN
    assert plan.schemas == (SCHEMA, ETL)
    assert plan.server_version != ""
    assert plan.objects_total == taken.done.objects_total == Fixture.OBJECTS
    batches = [f for f in taken.frames if f.kind == SyncFrameKind.BATCH.value]
    assert taken.done.batches == len(batches)
    assert len(batches) > len(Fixture.RELATIONS) / Fixture.BATCH_SIZE
    assert taken.artifact["metadata"]["objects"] == str(taken.done.objects_total)

    snapshot = taken.snapshot
    database = snapshot.databases[0]
    assert database.name == plan.database == connection.dbname
    assert {schema.name: schema.comment for schema in snapshot.schemas} == {
        SCHEMA: "snapshot fixture",
        ETL: None,
    }


async def test_relations_columns_constraints_and_indexes(taken: Taken) -> None:
    snapshot = taken.snapshot
    relations = {r.name: r for r in snapshot.relations}
    assert set(relations) == set(Fixture.RELATIONS)
    assert relations["customers"].kind is PgRelationKind.TABLE
    assert relations["customers"].comment == "people"
    assert relations["orders"].kind is PgRelationKind.PARTITIONED
    assert relations["orders"].partition_key == "RANGE (day)"
    assert relations["orders_2026"].kind is PgRelationKind.PARTITION
    assert relations["orders_2026"].partition_of == f"{SCHEMA}.orders"
    assert relations["orders_2026"].partition_bound is not None
    assert relations["v_orders"].kind is PgRelationKind.VIEW
    assert relations["v_orders"].definition is not None
    assert "customers c" in relations["v_orders"].definition
    assert relations["v_orders"].check_option is None
    assert relations["v_happy"].check_option == "local"
    assert relations["mv_totals"].kind is PgRelationKind.MATERIALIZED
    assert relations["mv_totals"].populated is True

    columns = {
        (c.relation, c.name): c for c in snapshot.columns if c.schema_name == SCHEMA
    }
    assert columns[("customers", "name")].comment == "full name"
    assert columns[("customers", "name")].nullable is False
    assert columns[("customers", "mood")].type == f"{SCHEMA}.mood"
    assert columns[("customers", "mood")].default == f"'ok'::{SCHEMA}.mood"
    assert columns[("customers", "note")].collation == "C"
    assert columns[("orders", "amount")].type == "numeric(12,2)"

    constraints = {
        (c.relation, c.kind): c
        for c in snapshot.constraints
        if c.relation in ("orders", "customers")
    }
    foreign = constraints[("orders", PgConstraintKind.FOREIGN)]
    assert foreign.columns == ("customer_id",)
    assert foreign.ref_relation == "customers"
    assert foreign.ref_columns == ("id",)
    assert foreign.on_delete == "cascade"
    assert foreign.on_update == "restrict"
    assert foreign.deferrable is True
    assert constraints[("orders", PgConstraintKind.PRIMARY)].columns == ("id", "day")
    check = constraints[("orders", PgConstraintKind.CHECK)]
    assert check.definition.startswith("CHECK")
    assert constraints[("customers", PgConstraintKind.PRIMARY)].columns == ("id",)

    indexes = {i.name: i for i in snapshot.indexes}
    assert indexes["customers_name_idx"].columns == ("lower(name)",)
    predicate = indexes["orders_day_idx"].predicate
    assert predicate is not None
    assert "amount > 0" in predicate
    assert indexes["customers_pkey"].primary is True


async def test_routines_sequences_and_types(taken: Taken) -> None:
    snapshot = taken.snapshot
    routines = {r.name: r for r in snapshot.routines}
    assert routines["load_orders"].kind is PgRoutineKind.FUNCTION
    assert routines["load_orders"].signature == "date, integer"
    assert routines["load_orders"].arguments == "day date, batch integer DEFAULT 100"
    assert routines["load_orders"].returns == "integer"
    assert routines["load_orders"].volatility == "stable"
    assert routines["load_orders"].comment == "loads a day"
    assert "select batch" in routines["load_orders"].body
    assert routines["load_orders"].definition.startswith("CREATE OR REPLACE FUNCTION")
    assert routines["refresh_totals"].kind is PgRoutineKind.PROCEDURE
    assert routines["refresh_totals"].returns is None
    assert routines["rows_of"].returns_set is True
    assert routines["rows_of"].strict is True
    assert routines["rows_of"].volatility == "immutable"

    args = {(a.routine, a.position): a for a in snapshot.routine_args}
    assert args[("load_orders", 0)].name == "day"
    assert args[("load_orders", 0)].type == "date"
    assert args[("load_orders", 1)].name == "batch"
    assert args[("refresh_totals", 0)].mode == "inout"

    sequences = {s.name: s for s in snapshot.sequences}
    assert sequences["orders_seq"].start == 10
    assert sequences["orders_seq"].increment == 5
    assert sequences["orders_seq"].owned_by == f"{SCHEMA}.orders.id"

    types = {t.name: t for t in snapshot.types}
    assert types["mood"].kind is PgTypeKind.ENUM
    assert types["mood"].labels == ("sad", "ok", "happy")
    assert types["positive_int"].kind is PgTypeKind.DOMAIN
    assert types["positive_int"].base_type == "integer"
    constraint = types["positive_int"].constraint
    assert constraint is not None
    assert constraint.startswith("CHECK")
    assert "VALUE > 0" in constraint
    assert types["point2"].kind is PgTypeKind.COMPOSITE
    assert types["point2"].attributes is not None
    assert [a.name for a in types["point2"].attributes] == ["x", "y"]


async def test_all_user_schemas_when_the_list_is_empty(
    connection: PostgresConfig,
) -> None:
    frames, _artifact = await _run_tool(connection, "", 50)

    plan, _done, snapshot = _assemble(frames)
    assert SCHEMA in plan.schemas
    assert ETL in plan.schemas
    assert "pg_catalog" not in plan.schemas
    assert "information_schema" not in plan.schemas
    assert {r.name for r in snapshot.relations} >= set(Fixture.RELATIONS)
