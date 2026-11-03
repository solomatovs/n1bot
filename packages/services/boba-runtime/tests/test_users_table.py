"""Таблица users приложения: своя схема и запросы слоя данных чата."""

from __future__ import annotations

import asyncio
import json
import secrets
import stat
import sys
from importlib.resources import files
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from psycopg import sql

from boba.chat.threads import ThreadUpsert
from boba.connection_broker.crypto import SecretCipher
from boba.connection_broker.store import ConnectionStore
from boba.connection_broker.tools import GrantedConnections
from boba.connections.manifest import ConnectionTypes
from boba.connections.stored import GrantTarget
from boba.db.postgres import AsyncPostgresPool, PostgresSchema
from boba.identity.api import UserIdentityAssignment, UserIdentityMigration
from boba.identity.context import Subject
from boba.identity.errors import ExternalServiceError
from boba.identity.principal import UserIdentity
from boba.identity.session import Login
from boba.identity.signin import SignedIn, SignInMetadata
from boba.runtime.config import RuntimeConfig
from boba.runtime.threads import ThreadsTable
from boba.runtime.users import UsersTable
from boba.stand.site import StandConfigEnv, StandConfigPaths

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
    async with pool.connection() as conn:
        await PostgresSchema(SCHEMA).apply_packages(
            conn, ("boba.identity_postgres", "boba.runtime")
        )

    return UsersTable(cfg, SCHEMA, pool)


async def test_setup_creates_the_schema_and_users_round_trip(users: UsersTable) -> None:
    signed = SignedIn(
        identifier=Login("corporate:reader"),
        display_name="reader",
        sign_in=SignInMetadata(
            identity=UserIdentity(realm="corporate", login=Login("reader")),
            roles=frozenset({"DEV"}),
        ),
    )
    created = await users.ensure_user(signed)
    found = await users.get_user(Login("corporate:reader"))
    assert found is not None
    assert found.id == created.id
    assert found.roles == frozenset({"DEV"})


@pytest.fixture
async def broker(users: UsersTable, pool: AsyncPostgresPool) -> ConnectionStore:
    async with pool.connection() as conn:
        await PostgresSchema(SCHEMA).apply_packages(
            conn, ("boba.identity_postgres", "boba.connection_broker")
        )

    return ConnectionStore(
        pool, SCHEMA, SecretCipher(secrets.token_bytes(32)), ConnectionTypes.discover()
    )


def signed(identity: UserIdentity) -> SignedIn:
    return SignedIn(
        identifier=identity.identifier,
        display_name=identity.login,
        sign_in=SignInMetadata(identity=identity),
    )


async def test_migration_preserves_history_and_personal_grants_and_can_roll_back(
    users: UsersTable,
    broker: ConnectionStore,
    pool: AsyncPostgresPool,
    runtime_config: RuntimeConfig,
) -> None:
    before = await users.upsert(Login("alice"), {"theme": "dark", "roles": ["read"]})
    threads = ThreadsTable(None, SCHEMA, pool)
    thread_id = uuid4()
    await threads.upsert(ThreadUpsert(id=thread_id, user_id=before.id, name="History"))
    connection = await broker.add("warehouse", runtime_config.postgres)
    grant_id = await broker.grant(connection, GrantTarget.user(before.id))
    identity = UserIdentity(realm="office", login=Login("alice"))
    request = UserIdentityMigration(
        users=(
            UserIdentityAssignment(
                user_id=before.id,
                previous_identifier=before.identifier,
                identity=identity,
            ),
        )
    )
    plan = await users.identity_snapshot(request)

    assert await users.stored(Login("alice")) == before
    await users.apply_identity_snapshot(plan)
    await users.apply_identity_snapshot(plan)
    after = await users.stored(identity.identifier)
    assert after is not None
    assert after.id == before.id
    assert after.meta["theme"] == "dark"
    history = await threads.get(thread_id)
    assert history is not None
    assert history.user_id == before.id
    assert (await broker.grant(connection, GrantTarget.user(before.id))) == grant_id

    other = await users.ensure_user(
        signed(UserIdentity(realm="aid-local", login=Login("alice")))
    )
    assert other.id != after.id
    catalog = GrantedConnections(lambda: broker)
    owner = Subject(
        user_id=after.id, login=identity.identifier, roles=frozenset(), profile="test"
    )
    stranger = Subject(
        user_id=other.id, login=other.identifier, roles=frozenset(), profile="test"
    )
    assert len((await catalog.rows(owner)).rows) == 1
    assert not (await catalog.rows(stranger)).rows

    await users.apply_identity_snapshot(plan, rollback=True)
    await users.apply_identity_snapshot(plan, rollback=True)
    assert await users.stored(Login("alice")) == before
    assert await users.stored(identity.identifier) is None
    assert (await broker.grant(connection, GrantTarget.user(before.id))) == grant_id


async def test_new_sign_in_does_not_claim_an_unmapped_legacy_user(
    users: UsersTable,
) -> None:
    legacy = await users.upsert(Login("alice"), {"roles": ["administrator"]})
    identity = UserIdentity(realm="office", login=Login("alice"))

    with pytest.raises(ExternalServiceError, match="explicit identity mapping"):
        await users.ensure_user(signed(identity))

    assert await users.stored(Login("alice")) == legacy
    assert await users.stored(identity.identifier) is None


async def test_a_legacy_login_that_looks_namespaced_is_not_claimed(
    users: UsersTable,
) -> None:
    legacy = await users.upsert(Login("office:alice"), {"roles": ["administrator"]})
    identity = UserIdentity(realm="office", login=Login("alice"))

    with pytest.raises(ExternalServiceError, match="explicit identity mapping"):
        await users.ensure_user(signed(identity))

    assert await users.stored_by_id(legacy.id) == legacy


async def test_stale_migration_is_atomic(users: UsersTable) -> None:
    alice = await users.upsert(Login("alice"), {})
    bob = await users.upsert(Login("bob"), {})
    assignments = []
    for user in (alice, bob):
        assignments.append(
            UserIdentityAssignment(
                user_id=user.id,
                previous_identifier=user.identifier,
                identity=UserIdentity(realm="office", login=user.identifier),
            )
        )
    plan = await users.identity_snapshot(
        UserIdentityMigration(users=tuple(assignments))
    )
    await users.upsert(bob.identifier, {"theme": "changed"})

    with pytest.raises(ValueError, match="changed after"):
        await users.apply_identity_snapshot(plan)

    assert await users.stored(alice.identifier) == alice
    assert await users.stored(Login("office:alice")) is None


async def test_migration_does_not_merge_two_existing_users(users: UsersTable) -> None:
    legacy = await users.upsert(Login("alice"), {})
    identity = UserIdentity(realm="office", login=Login("alice"))
    existing = await users.upsert(identity.identifier, {})
    assignment = UserIdentityAssignment(
        user_id=legacy.id, previous_identifier=legacy.identifier, identity=identity
    )

    with pytest.raises(ValueError, match="already belongs"):
        await users.identity_snapshot(UserIdentityMigration(users=(assignment,)))

    assert await users.stored_by_id(legacy.id) == legacy
    assert await users.stored_by_id(existing.id) == existing


async def test_import_retains_the_old_mcp_workspace_uuid(users: UsersTable) -> None:
    workspace_owner = UUID("5c0447be-5531-404a-814b-693578c60098")
    identity = UserIdentity(realm="office", login=Login("alice"))
    request = UserIdentityMigration(
        seed_missing=True,
        users=(
            UserIdentityAssignment(
                user_id=workspace_owner,
                previous_identifier=Login("alice"),
                identity=identity,
            ),
        ),
    )
    plan = await users.identity_snapshot(request)
    await users.apply_identity_snapshot(plan)
    admitted = await users.ensure_user(signed(identity))

    assert admitted.id == workspace_owner
    with pytest.raises(ValueError, match="changed after"):
        await users.apply_identity_snapshot(plan, rollback=True)
    assert await users.stored_by_id(workspace_owner) is not None


async def test_users_schema_can_be_initialized_without_chat_tables(
    pool: AsyncPostgresPool,
) -> None:
    schema = PostgresSchema(f"{SCHEMA}_mcp")
    async with pool.connection() as conn:
        applied = await schema.apply_files(
            conn,
            files("boba.identity_postgres") / "schema",
            {},
            only=frozenset({"10_users.sql"}),
        )
        assert applied == ["10_users.sql"]
        assert await schema.has_table(conn, "users")
        assert not await schema.has_table(conn, "threads")
        assert not await schema.has_table(conn, "feedbacks")


async def test_migration_cli_plans_applies_and_rolls_back_the_same_uuid(
    users: UsersTable, tmp_path: Path, test_database: str
) -> None:
    before = await users.upsert(Login("alice"), {"theme": "dark"})
    overlay = tmp_path / "storage.toml"
    overlay.write_text(
        f'[app]\nschema = "{SCHEMA}"\n'
        f"[postgres]\ndbname = {json.dumps(test_database)}\n"
        f"[instance]\ndata_dir = {json.dumps(str(tmp_path / 'instance'))}\n",
        encoding="utf-8",
    )
    mapping = tmp_path / "mapping.toml"
    mapping.write_text(
        "[migration]\n[[migration.users]]\n"
        f'user_id = "{before.id}"\nprevious_identifier = "alice"\n'
        '[migration.users.identity]\nrealm = "office"\nlogin = "alice"\n',
        encoding="utf-8",
    )
    plan = tmp_path / "plan.json"
    arguments = [sys.executable, "-m", "boba.runtime.identities"]
    for path in (*StandConfigPaths(StandConfigEnv.RUN).paths(), overlay):
        arguments.extend(("--config", str(path)))

    async def invoke(*operation: str) -> None:
        process = await asyncio.create_subprocess_exec(
            *arguments,
            *operation,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, error = await process.communicate()
        assert process.returncode == 0, error.decode()

    await invoke("plan", str(mapping), str(plan))
    assert stat.S_IMODE(plan.stat().st_mode) == 0o600
    assert await users.stored(before.identifier) == before
    await invoke("apply", str(plan))
    migrated = await users.stored(Login("office:alice"))
    assert migrated is not None
    assert migrated.id == before.id
    await invoke("rollback", str(plan))
    assert await users.stored(before.identifier) == before


async def test_sign_in_synchronizes_role_membership_without_changing_the_user(
    users: UsersTable,
    pool: AsyncPostgresPool,
) -> None:
    identity = UserIdentity(realm="office", login=Login("alice"))
    first = signed(identity).model_copy(
        update={
            "sign_in": SignInMetadata(
                identity=identity, roles=frozenset({"reader", "writer"})
            )
        }
    )
    created = await users.ensure_user(first)
    changed = first.model_copy(
        update={
            "sign_in": SignInMetadata(identity=identity, roles=frozenset({"reader"}))
        }
    )
    updated = await users.ensure_user(changed)
    assert updated.id == created.id
    query = sql.SQL("""
        select r.role from {}.user_roles as m
        join {}.roles as r on r.id = m.role_id
        where m.user_id = %s order by r.role
    """).format(sql.Identifier(SCHEMA), sql.Identifier(SCHEMA))
    async with pool.connection() as conn:
        memberships = await (await conn.execute(query, (created.id,))).fetchall()
    assert memberships == [("reader",)]


async def test_sign_in_without_roles_clears_previous_membership(
    users: UsersTable,
    pool: AsyncPostgresPool,
) -> None:
    identity = UserIdentity(realm="office", login=Login("alice"))
    privileged = signed(identity).model_copy(
        update={
            "sign_in": SignInMetadata(
                identity=identity,
                roles=frozenset({"writer"}),
                profiles=frozenset({"privileged"}),
            )
        }
    )
    created = await users.ensure_user(privileged)
    without_roles = await users.ensure_user(signed(identity))
    assert without_roles.id == created.id
    assert not without_roles.roles
    assert not without_roles.profiles
    query = sql.SQL("select count(*) from {}.user_roles where user_id = %s").format(
        sql.Identifier(SCHEMA)
    )
    async with pool.connection() as conn:
        row = await (await conn.execute(query, (created.id,))).fetchone()
    assert row == (0,)
