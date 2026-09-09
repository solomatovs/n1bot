"""connection_list на живой базе: строки connections/roles/grants и субъект.

Строки и гранты кладутся хранилищем брокера (как их положит приложение),
выборка идёт телом инструмента своим подключением к той же базе.
"""

from __future__ import annotations

import base64
import secrets
from collections.abc import Mapping, Sequence
from uuid import uuid4

import pytest
from psycopg import sql
from pydantic import SecretStr

from boba.connection_broker.store import ConnectionsConfig, ConnectionStore
from boba.connections.manifest import ConnectionTypes
from boba.connections.profile import GrantTarget
from boba.db.postgres import AsyncPostgresPool
from boba.db.postgres.profile import (
    PasswordAuth,
    PostgresConfig,
    PostgresOptionsConfig,
    PostgresPoolConfig,
)
from boba.identity.context import Subject
from boba.tool.connections.tools import (
    CatalogColumn,
    ConnectionsToolConfig,
    GrantedConnections,
)

pytestmark = [pytest.mark.anyio, pytest.mark.integration]

SCHEMA = "connections_tool"
ROLE = "analyst"


@pytest.fixture
async def store(pool: AsyncPostgresPool) -> ConnectionStore:
    async with pool.connection() as conn:
        await conn.execute(
            sql.SQL("drop schema if exists {} cascade").format(sql.Identifier(SCHEMA))
        )

    key = SecretStr(base64.b64encode(secrets.token_bytes(32)).decode())
    cfg = ConnectionsConfig(enable=True, db_schema=SCHEMA, encryption_key=key)
    built = ConnectionStore(cfg, ConnectionTypes.discover(), pool)
    await built.setup()
    await built.sync_roles([ROLE])
    return built


@pytest.fixture
def catalog(test_postgres: PostgresConfig) -> GrantedConnections:
    cfg = ConnectionsToolConfig(connection=test_postgres, db_schema=SCHEMA)
    return GrantedConnections(cfg)


def _subject(roles: frozenset[str] = frozenset()) -> Subject:
    return Subject(user_id=uuid4(), login="tester", roles=roles, profile="test")


def _pg(description: str) -> PostgresConfig:
    return PostgresConfig(
        host="db.example",
        dbname="app",
        description=description,
        auth=PasswordAuth(method="password", user="app", password=SecretStr("x")),
        options=PostgresOptionsConfig(),
        pool=PostgresPoolConfig(),
    )


def _names(rows: Sequence[Mapping[str, object]]) -> list[object]:
    names: list[object] = []
    for row in rows:
        names.append(row[CatalogColumn.NAME])

    return names


async def test_personal_and_role_grants_are_listed(
    store: ConnectionStore, catalog: GrantedConnections
) -> None:
    subject = _subject(frozenset({ROLE}))
    role_id = (await store.roles())[0].id

    mine = await store.add("mine", _pg("personal"))
    await store.grant(mine, GrantTarget.user(subject.user_id))

    shared = await store.add("shared", _pg("by role"))
    await store.grant(shared, GrantTarget.role(role_id))

    stranger = await store.add("stranger", _pg("someone else's"))
    await store.grant(stranger, GrantTarget.user(uuid4()))

    result = await catalog.rows(subject)

    if _names(result.rows) != ["mine", "shared"]:
        raise AssertionError(f"granted rows only, sorted by name: {result.rows}")

    if result.rows[0][CatalogColumn.KIND] != "postgres":
        raise AssertionError(f"kind comes from the profile: {result.rows[0]}")

    if result.rows[1][CatalogColumn.DESCRIPTION] != "by role":
        raise AssertionError(f"description comes from the profile: {result.rows[1]}")


async def test_role_grant_needs_the_role(
    store: ConnectionStore, catalog: GrantedConnections
) -> None:
    role_id = (await store.roles())[0].id
    shared = await store.add("shared", _pg("by role"))
    await store.grant(shared, GrantTarget.role(role_id))

    result = await catalog.rows(_subject())

    if result.rows != []:
        raise AssertionError(f"no role, no rows: {result.rows}")

    if result.note != GrantedConnections.EMPTY_NOTE:
        raise AssertionError(f"empty list carries a note: {result.note!r}")


async def test_duplicate_name_within_a_kind_is_hidden(
    store: ConnectionStore, catalog: GrantedConnections
) -> None:
    subject = _subject(frozenset({ROLE}))
    role_id = (await store.roles())[0].id

    personal = await store.add("main", _pg("mine"))
    await store.grant(personal, GrantTarget.user(subject.user_id))

    by_role = await store.add("main", _pg("by role"))
    await store.grant(by_role, GrantTarget.role(role_id))

    other = await store.add("other", _pg("unique"))
    await store.grant(other, GrantTarget.user(subject.user_id))

    result = await catalog.rows(subject)

    if _names(result.rows) != ["other"]:
        raise AssertionError(f"ambiguous name must not be listed: {result.rows}")
