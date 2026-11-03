"""Явная миграция пустой схемы на реальном PostgreSQL стенда."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from uuid import uuid4

import pytest
from psycopg import sql

from boba.db.postgres import AsyncPostgresPool, PostgresError, PostgresSchema
from boba.db.postgres.connection import PostgresConfig
from boba.db.postgres.migrate import (
    MigrationScript,
    MigrationScripts,
    MigrationTarget,
    PostgresMigrations,
)

pytestmark = [pytest.mark.integration, pytest.mark.anyio]


@pytest.fixture
async def schema(pool: AsyncPostgresPool) -> AsyncIterator[str]:
    name = f"migrate_{uuid4().hex[:12]}"
    try:
        yield name
    finally:
        async with pool.connection() as conn:
            await conn.execute(
                sql.SQL("drop schema if exists {} cascade").format(sql.Identifier(name))
            )


async def test_creates_tables_and_refuses_repeat_without_changing_data(
    test_postgres: PostgresConfig, schema: str, pool: AsyncPostgresPool
) -> None:
    plan = MigrationScripts(schema).packages(("boba.identity_postgres",))
    target = MigrationTarget(test_postgres, plan)
    await PostgresMigrations().apply((target, target))
    async with pool.connection() as conn:
        query = sql.SQL("insert into {}.roles (role) values ('reader')").format(
            sql.Identifier(schema)
        )
        await conn.execute(query)

    with pytest.raises(
        PostgresError, match="existing tables: roles, user_roles, users"
    ):
        await PostgresMigrations().apply((target,))

    async with pool.connection() as conn:
        row = await (
            await conn.execute(
                sql.SQL("select role from {}.roles").format(sql.Identifier(schema))
            )
        ).fetchone()
    assert row == ("reader",)


async def test_checks_every_table_before_creating_anything(
    test_postgres: PostgresConfig, schema: str, pool: AsyncPostgresPool
) -> None:
    async with pool.connection() as conn:
        await conn.execute(sql.SQL("create schema {}").format(sql.Identifier(schema)))
        await conn.execute(
            sql.SQL("create table {}.unexpected (id int)").format(
                sql.Identifier(schema)
            )
        )

    plan = MigrationScripts(schema).packages(("boba.identity_postgres",))
    with pytest.raises(PostgresError, match="existing tables: unexpected"):
        await PostgresMigrations().apply((MigrationTarget(test_postgres, plan),))

    async with pool.connection() as conn:
        assert not await PostgresSchema(schema).has_table(conn, "users")


async def test_rolls_back_all_scripts_if_the_last_one_fails(
    test_postgres: PostgresConfig, schema: str, pool: AsyncPostgresPool
) -> None:
    plan = MigrationScripts(schema).packages(("boba.identity_postgres",))
    broken = MigrationScript(name="broken.sql", text="select 1 / 0")
    plan = plan.model_copy(update={"scripts": (*plan.scripts, broken)})
    with pytest.raises(PostgresError, match="division by zero"):
        await PostgresMigrations().apply((MigrationTarget(test_postgres, plan),))

    async with pool.connection() as conn:
        assert not await PostgresSchema(schema).has_table(conn, "users")


async def test_shared_schema_combines_history_and_identity_once(
    test_postgres: PostgresConfig, schema: str, pool: AsyncPostgresPool
) -> None:
    identity = MigrationScripts(schema).packages(("boba.identity_postgres",))
    history = MigrationScripts(schema).packages(("boba.history.postgres",))
    targets = (
        MigrationTarget(test_postgres, identity),
        MigrationTarget(test_postgres, history),
        MigrationTarget(test_postgres, history),
    )
    applied = await PostgresMigrations().apply(targets)
    assert len(applied) == 2
    async with pool.connection() as conn:
        assert await PostgresSchema(schema).has_table(conn, "session_records")


async def test_parallel_migrations_have_one_winner(
    test_postgres: PostgresConfig, schema: str
) -> None:
    plan = MigrationScripts(schema).packages(("boba.identity_postgres",))
    target = MigrationTarget(test_postgres, plan)
    results = await asyncio.gather(
        PostgresMigrations().apply((target,)),
        PostgresMigrations().apply((target,)),
        return_exceptions=True,
    )
    failures = [result for result in results if isinstance(result, PostgresError)]
    assert len(failures) == 1
    assert "expected an empty schema" in str(failures[0])


async def test_all_schemas_are_checked_before_any_ddl(
    test_postgres: PostgresConfig, schema: str, pool: AsyncPostgresPool
) -> None:
    dirty = MigrationScripts(schema).packages(("boba.identity_postgres",))
    clean_name = f"a_{schema}_clean"
    clean = MigrationScripts(clean_name).packages(("boba.identity_postgres",))
    await PostgresMigrations().apply((MigrationTarget(test_postgres, dirty),))
    with pytest.raises(PostgresError, match="existing tables"):
        await PostgresMigrations().apply(
            (
                MigrationTarget(test_postgres, clean),
                MigrationTarget(test_postgres, dirty),
            )
        )

    async with pool.connection() as conn:
        assert not await PostgresSchema(clean_name).has_table(conn, "users")
