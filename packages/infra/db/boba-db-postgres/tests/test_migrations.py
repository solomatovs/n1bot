"""Общий загрузчик исполняет только упорядоченные SQL скрипты владельца."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from uuid import uuid4

import pytest
from psycopg import sql

from boba.db.postgres import AsyncPostgresPool, PostgresError, PostgresSchema
from boba.db.postgres.connection import PostgresConfig
from boba.db.postgres.migrate import (
    MigrationPlan,
    MigrationScript,
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


def _plan(schema: str, component: str = "migration-test") -> MigrationPlan:
    scripts = (
        MigrationScript(
            name="10_known_table.sql",
            text="""
                create table if not exists {schema}.known_table (
                    id integer primary key,
                    value text not null
                )
            """,
        ),
        MigrationScript(
            name="20_known_column.sql",
            text="""
                alter table {schema}.known_table
                    add column if not exists marker text not null default 'ready'
            """,
        ),
    )
    return MigrationPlan(
        schema=schema,
        component=component,
        version="0001",
        scripts=scripts,
    )


async def test_sql_plan_is_repeatable_and_leaves_unknown_tables_alone(
    test_postgres: PostgresConfig, schema: str, pool: AsyncPostgresPool
) -> None:
    target = MigrationTarget(test_postgres, _plan(schema))
    runner = PostgresMigrations()
    await runner.apply((target,))

    async with pool.connection() as conn:
        await conn.execute(
            sql.SQL("insert into {}.known_table (id, value) values (1, 'kept')").format(
                sql.Identifier(schema)
            )
        )
        await conn.execute(
            sql.SQL("create table {}.unowned (id integer primary key)").format(
                sql.Identifier(schema)
            )
        )
        await conn.execute(
            sql.SQL("insert into {}.unowned (id) values (7)").format(
                sql.Identifier(schema)
            )
        )

    await runner.apply((target,))

    async with pool.connection() as conn:
        known = await (
            await conn.execute(
                sql.SQL("select id, value, marker from {}.known_table").format(
                    sql.Identifier(schema)
                )
            )
        ).fetchone()
        unowned = await (
            await conn.execute(
                sql.SQL("select id from {}.unowned").format(sql.Identifier(schema))
            )
        ).fetchone()

    assert known == (1, "kept", "ready")
    assert unowned == (7,)


async def test_failed_sql_rolls_back_the_whole_plan(
    test_postgres: PostgresConfig, schema: str
) -> None:
    plan = _plan(schema).model_copy(
        update={
            "scripts": (
                *_plan(schema).scripts,
                MigrationScript(name="30_broken.sql", text="select 1 / 0"),
            )
        }
    )

    with pytest.raises(PostgresError, match="division by zero"):
        await PostgresMigrations().apply((MigrationTarget(test_postgres, plan),))

    async with await AsyncPostgresPool.dedicated(test_postgres) as conn:
        assert not await PostgresSchema(schema).has_table(conn, "known_table")


async def test_repeated_target_in_one_run_executes_once(
    test_postgres: PostgresConfig, schema: str
) -> None:
    target = MigrationTarget(test_postgres, _plan(schema))

    applied = await PostgresMigrations().apply((target, target))

    assert len(applied) == 2


async def test_concurrent_idempotent_plans_both_succeed(
    test_postgres: PostgresConfig, schema: str, pool: AsyncPostgresPool
) -> None:
    target = MigrationTarget(test_postgres, _plan(schema))
    results = await asyncio.gather(
        PostgresMigrations().apply((target,)),
        PostgresMigrations().apply((target,)),
        return_exceptions=True,
    )

    failures = [result for result in results if isinstance(result, BaseException)]
    assert failures == []
    async with pool.connection() as conn:
        assert await PostgresSchema(schema).has_table(conn, "known_table")


async def test_each_target_uses_its_configured_schema(
    test_postgres: PostgresConfig, schema: str, pool: AsyncPostgresPool
) -> None:
    second = f"{schema}_second"
    targets = (
        MigrationTarget(test_postgres, _plan(schema, "first-owner")),
        MigrationTarget(test_postgres, _plan(second, "second-owner")),
    )

    await PostgresMigrations().apply(targets)

    async with pool.connection() as conn:
        assert await PostgresSchema(schema).has_table(conn, "known_table")
        assert await PostgresSchema(second).has_table(conn, "known_table")
        await conn.execute(
            sql.SQL("drop schema if exists {} cascade").format(sql.Identifier(second))
        )
