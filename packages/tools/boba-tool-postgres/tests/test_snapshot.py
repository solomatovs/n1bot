"""pg_schema_snapshot против живого Postgres: схема со всеми видами объектов
перетекает COPY-потоком в домен каталога той же тестовой базы, версия
читается обратно в PgSnapshot, и в нём есть всё, что было создано: таблицы
с ключами и индексами, секции, view, материализованное view, функции и
процедуры с аргументами, последовательность с владельцем, enum, domain и
composite."""

from __future__ import annotations

from typing import Any, ClassVar
from uuid import UUID

import pytest
from psycopg import sql
from psycopg.rows import dict_row

from boba.catalog import SourceKinds
from boba.db.postgres import AsyncPostgresPool
from boba.db.postgres.catalog import (
    CatalogDomain,
    CatalogStoreConfig,
    SnapshotOutcome,
    SnapshotReader,
    SnapshotTables,
    StagingTable,
)
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

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

SCHEMA = "snap_test"
ETL = "snap_etl"
CATALOG = "snap_catalog"
CONNECTION_ID = UUID(int=0x5AA5)
KINDS = SourceKinds.of(PgSnapshot)


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


class Domain:
    """Схема домена каталога в тестовой базе: таблицы pg_* по раскладке
    моделей, чтение версии обратно."""

    @classmethod
    async def prepare(cls, pool: AsyncPostgresPool) -> None:
        async with pool.connection() as conn:
            await conn.execute(
                sql.SQL("drop schema if exists {} cascade").format(
                    sql.Identifier(CATALOG)
                )
            )
            await conn.execute(
                sql.SQL("create schema {}").format(sql.Identifier(CATALOG))
            )
            domain = CatalogDomain(CATALOG, SnapshotTables(KINDS))
            for statement in domain.ddl():
                await conn.execute(statement, prepare=False)

    @classmethod
    async def read(cls, pool: AsyncPostgresPool, version: int) -> PgSnapshot:
        reader = SnapshotReader(CATALOG, KINDS)
        async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            snapshot = await reader.read(cur, CONNECTION_ID, "postgres", version)

        if not isinstance(snapshot, PgSnapshot):
            raise AssertionError(
                f"expected a PgSnapshot, got {type(snapshot).__name__}"
            )

        return snapshot

    @classmethod
    async def staging(cls, pool: AsyncPostgresPool) -> list[str]:
        async with pool.connection() as conn, conn.cursor() as cur:
            pattern = StagingTable.pattern_of(CONNECTION_ID)
            return await StagingTable.names_in(cur, CATALOG, pattern)


async def _run_tool(
    connection: PostgresConfig, schemas: str
) -> tuple[SnapshotOutcome, dict[str, Any]]:
    body = ToolMain.toolset(pg_schema_snapshot)[0].coroutine
    if body is None:
        raise AssertionError("pg_schema_snapshot has no coroutine body")

    _content, artifact = await body(
        connection=connection.identified(CONNECTION_ID, "snap"),
        schemas=schemas,
        catalog=CatalogStoreConfig(connection=connection, db_schema=CATALOG),
    )
    return SnapshotOutcome.of_result(artifact), artifact.model_dump(mode="json")


@pytest.fixture
async def connection(
    runtime_config: RuntimeConfig, test_database: str, pool: AsyncPostgresPool
) -> PostgresConfig:
    await Fixture.prepare(pool)
    await Domain.prepare(pool)
    return TestDatabase.config_of(runtime_config.data_layer.postgres, test_database)


class Taken:
    """Один прогон инструмента: итог, артефакт и снимок версии из домена."""

    def __init__(
        self, outcome: SnapshotOutcome, artifact: dict[str, Any], snapshot: PgSnapshot
    ) -> None:
        self.outcome = outcome
        self.artifact = artifact
        self.snapshot = snapshot


@pytest.fixture
async def taken(connection: PostgresConfig, pool: AsyncPostgresPool) -> Taken:
    outcome, artifact = await _run_tool(connection, f"{SCHEMA}, {ETL}")
    snapshot = await Domain.read(pool, outcome.version)
    return Taken(outcome, artifact, snapshot)


async def test_version_lands_in_the_domain(
    taken: Taken, connection: PostgresConfig, pool: AsyncPostgresPool
) -> None:
    assert taken.outcome.version == 1
    assert taken.outcome.server_version != ""
    assert taken.artifact["metadata"]["objects"] == str(Fixture.OBJECTS)
    assert taken.artifact["metadata"]["schemas"] == f"{SCHEMA}, {ETL}"
    assert await Domain.staging(pool) == []

    snapshot = taken.snapshot
    snapshot.check()
    assert snapshot.objects_count() == Fixture.OBJECTS
    database = snapshot.databases[0]
    assert database.name == connection.dbname
    assert {schema.name: schema.comment for schema in snapshot.schemas} == {
        SCHEMA: "snapshot fixture",
        ETL: None,
    }

    again, _artifact = await _run_tool(connection, f"{SCHEMA}, {ETL}")
    assert again.version == 2
    assert (await Domain.read(pool, 2)).objects_count() == Fixture.OBJECTS


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
    connection: PostgresConfig, pool: AsyncPostgresPool
) -> None:
    outcome, artifact = await _run_tool(connection, "")

    schemas = str(artifact["metadata"]["schemas"]).split(", ")
    assert SCHEMA in schemas
    assert ETL in schemas
    assert "pg_catalog" not in schemas
    assert "information_schema" not in schemas
    snapshot = await Domain.read(pool, outcome.version)
    assert {r.name for r in snapshot.relations} >= set(Fixture.RELATIONS)
