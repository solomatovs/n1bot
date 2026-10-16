"""Таблица users приложения: своя схема и запросы слоя данных чата."""

from __future__ import annotations

from importlib.resources import files

import pytest
from psycopg import sql

from boba.db.postgres import AsyncPostgresPool, PostgresSchema
from boba.identity.session import Login
from boba.identity.signin import SignedIn, SignInMetadata
from boba.runtime.config import RuntimeConfig
from boba.runtime.users import UsersTable

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

SCHEMA = "automation_test"


@pytest.fixture
async def users(
    runtime_config: RuntimeConfig, test_database: str, pool: AsyncPostgresPool
) -> UsersTable:
    async with pool.connection() as conn:
        await conn.execute(
            sql.SQL("drop schema if exists {} cascade").format(sql.Identifier(SCHEMA))
        )

    cfg = runtime_config.postgres.model_copy(update={"dbname": test_database})
    schema_dir = files("boba.runtime") / "schema"
    async with pool.connection() as conn:
        await PostgresSchema(SCHEMA).apply_files(conn, schema_dir, {})

    return UsersTable(cfg, SCHEMA, pool)


async def test_setup_creates_the_schema_and_users_round_trip(users: UsersTable) -> None:
    signed = SignedIn(
        identifier=Login("reader"),
        display_name="reader",
        sign_in=SignInMetadata(roles=frozenset({"DEV"})),
    )
    created = await users.ensure_user(signed)
    found = await users.get_user(Login("reader"))
    assert found is not None
    assert found.id == created.id
    assert found.roles == frozenset({"DEV"})
