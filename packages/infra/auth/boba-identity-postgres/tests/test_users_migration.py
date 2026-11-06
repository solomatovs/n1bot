"""Миграция описывает только целевую схему identity и повторно сверяется."""

from __future__ import annotations

from uuid import uuid4

import pytest
from psycopg import sql

from boba.db.postgres import AsyncPostgresPool
from boba.db.postgres.connection import PostgresConfig
from boba.identity.principal import UserIdentity
from boba.identity_postgres import UsersTable
from boba.stand.migrate import StandMigrations

pytestmark = pytest.mark.anyio


async def test_migration_creates_normalized_identity_schema(
    pool: AsyncPostgresPool, test_postgres: PostgresConfig
) -> None:
    schema = f"identity_test_{uuid4().hex[:12]}"
    migrations = StandMigrations()

    try:
        await migrations.application(test_postgres, schema, "boba.identity_postgres")
        await migrations.application(test_postgres, schema, "boba.identity_postgres")

        async with pool.connection() as conn:
            rows = await (
                await conn.execute(
                    "select column_name from information_schema.columns "
                    "where table_schema = %s and table_name = 'users'",
                    (schema,),
                )
            ).fetchall()

        columns = {row[0] for row in rows}
        assert columns == {"id", "created_at", "login"}

        identity = UserIdentity.model_validate("alice")
        created = await UsersTable(pool, schema).upsert(identity)
        stored = await UsersTable(pool, schema).stored(identity)

        assert stored is not None
        assert stored.id == created.id
        assert stored.identity == identity
    finally:
        async with pool.connection() as conn:
            await conn.execute(
                sql.SQL("drop schema if exists {} cascade").format(
                    sql.Identifier(schema)
                )
            )
