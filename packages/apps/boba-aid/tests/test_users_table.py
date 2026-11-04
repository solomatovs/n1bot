"""AID's users table stores identity and role membership only."""

from __future__ import annotations

import pytest
from psycopg import sql

from boba.aid.data.users import UsersTable
from boba.aid.infra.config import AppConfig
from boba.db.postgres import AsyncPostgresPool
from boba.identity.principal import UserIdentity
from boba.identity.session import Login
from boba.stand.migrate import StandMigrations
from boba.stand.roles import RoleSeed

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

SCHEMA = "aid_users_test"


@pytest.fixture
async def users(
    runtime_config: AppConfig, test_database: str, pool: AsyncPostgresPool
) -> UsersTable:
    async with pool.connection() as conn:
        await conn.execute(
            sql.SQL("drop schema if exists {} cascade").format(sql.Identifier(SCHEMA))
        )

    config = runtime_config.postgres.model_copy(update={"dbname": test_database})
    await StandMigrations().application(config, SCHEMA, "boba.aid")
    return UsersTable(pool, SCHEMA)


async def test_users_round_trip_keeps_identity_and_existing_roles(
    users: UsersTable,
) -> None:
    await RoleSeed(users).add(("DEV",))
    identity = UserIdentity(realm="corporate", login=Login("Reader"))

    created = await users.upsert(identity, frozenset({"DEV"}))
    found = await users.stored(identity)

    assert found == created
    assert found is not None
    assert found.identity == UserIdentity(realm="corporate", login=Login("reader"))
    assert found.roles == {"DEV"}
    assert await users.stored_by_id(created.id) == created


async def test_same_login_in_two_realms_is_two_users(users: UsersTable) -> None:
    first = await users.upsert(UserIdentity(realm="office", login=Login("alice")))
    second = await users.upsert(UserIdentity(realm="partner", login=Login("alice")))

    assert first.id != second.id
    assert first.identifier == "office:alice"
    assert second.identifier == "partner:alice"


async def test_automatic_role_sync_preserves_manual_membership(
    users: UsersTable, pool: AsyncPostgresPool
) -> None:
    await RoleSeed(users).add(("read", "admin", "writer"))
    identity = UserIdentity(realm="corporate", login=Login("alice"))
    stored = await users.upsert(identity, frozenset({"read", "writer"}))
    async with pool.connection() as conn:
        await conn.execute(
            sql.SQL(
                "insert into {}.user_roles (user_id, role_id, managed) "
                "select %s, id, false from {}.roles where role = %s"
            ).format(sql.Identifier(SCHEMA), sql.Identifier(SCHEMA)),
            (stored.id, "admin"),
        )

    updated = await users.upsert(identity, frozenset({"read"}))

    assert updated.id == stored.id
    assert updated.roles == {"admin", "read"}
    assert await users.resolve_roles(identity, ("admin", "missing")) == {"admin"}


async def test_empty_automatic_membership_removes_only_managed_roles(
    users: UsersTable,
) -> None:
    await RoleSeed(users).add(("read",))
    identity = UserIdentity(realm="corporate", login=Login("alice"))

    first = await users.upsert(identity, frozenset({"read"}))
    cleared = await users.upsert(identity, frozenset())

    assert cleared.id == first.id
    assert cleared.roles == frozenset()


async def test_users_schema_has_no_metadata_or_tags_columns(
    users: UsersTable, pool: AsyncPostgresPool
) -> None:
    async with pool.connection() as conn:
        cursor = await conn.execute(
            "select column_name from information_schema.columns "
            "where table_schema = %s and table_name = 'users'",
            (SCHEMA,),
        )
        columns = {row[0] for row in await cursor.fetchall()}

    assert {"id", "created_at", "realm", "login"} <= columns
    assert not {"metadata", "tags"} & columns
