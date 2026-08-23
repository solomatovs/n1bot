"""ConnectionStore на реальном postgres: строки, гранты, выборка по субъекту."""

from __future__ import annotations

import base64
import json
import secrets as std_secrets

import pytest
from conftest import FakeSecret
from psycopg import sql
from pydantic import SecretStr

from boba.chainlit.connections import (
    ConnectionKind,
    ConnectionNotFoundError,
    ConnectionsConfig,
    ConnectionStore,
    GrantTarget,
    SecretCryptoError,
    Subject,
)
from boba.db.clickhouse import ClickHouseConfig, ClickHouseSettingsConfig
from boba.db.postgres import (
    AsyncPostgresPool,
    PostgresConfig,
    PostgresOptionsConfig,
    PostgresPoolConfig,
)
from boba.transport.http import HttpProfile
from boba.transport.http.auth import BearerAuth

pytestmark = pytest.mark.anyio

SCHEMA = "connections_test"


def _key() -> SecretStr:
    return SecretStr(base64.b64encode(std_secrets.token_bytes(32)).decode())


def _config(key: SecretStr) -> ConnectionsConfig:
    return ConnectionsConfig(enable=True, db_schema=SCHEMA, encryption_key=key)


def _pg(password: str) -> PostgresConfig:
    return PostgresConfig(
        host="db",
        user="boba",
        dbname="n1bot",
        gssencmode="disable",
        password=SecretStr(password),
        options=PostgresOptionsConfig(),
        pool=PostgresPoolConfig(),
    )


def _ch() -> ClickHouseConfig:
    return ClickHouseConfig(
        host="ch",
        port=8123,
        interface="http",
        username="boba",
        settings=ClickHouseSettingsConfig(),
    )


def _web(token: str) -> HttpProfile:
    return HttpProfile(
        base_url="https://confl",
        auth=BearerAuth(method="bearer", token=SecretStr(token)),
    )


@pytest.fixture
async def store(pool: AsyncPostgresPool) -> ConnectionStore:
    async with pool.connection() as conn:
        await conn.execute(
            sql.SQL("drop schema if exists {} cascade").format(sql.Identifier(SCHEMA))
        )

    built = ConnectionStore(_config(_key()), pool)
    await built.setup()
    return built


async def test_setup_is_idempotent(store: ConnectionStore) -> None:
    await store.setup()
    await store.setup()


async def test_add_and_get_restores_profile(store: ConnectionStore) -> None:
    connection_id = await store.add("main", _pg(FakeSecret.DB))

    stored = await store.get(connection_id)

    if stored.name != "main":
        raise AssertionError("name must survive the roundtrip")
    if stored.kind is not ConnectionKind.POSTGRES:
        raise AssertionError("kind must follow the profile")
    if not isinstance(stored.profile, PostgresConfig):
        raise AssertionError("profile must come back as PostgresConfig")
    if stored.profile.password is None:
        raise AssertionError("password must be restored")
    if stored.profile.password.get_secret_value() != FakeSecret.DB:
        raise AssertionError("password must be decrypted")


async def test_same_name_twice_is_allowed(store: ConnectionStore) -> None:
    first = await store.add("main", _pg(FakeSecret.DB))
    second = await store.add("main", _pg(FakeSecret.DB_OTHER))

    if first == second:
        raise AssertionError("two rows must get two ids")

    names = [stored.name for stored in await store.list_all()]
    if names != ["main", "main"]:
        raise AssertionError("name is not unique in the table")


async def test_secret_is_ciphertext_in_the_table(
    store: ConnectionStore, pool: AsyncPostgresPool
) -> None:
    await store.add("main", _pg(FakeSecret.DB))

    async with pool.cursor() as cur:
        await cur.execute(
            sql.SQL("select data from {}").format(
                sql.Identifier(SCHEMA, "connections")
            )
        )
        row = await cur.fetchone()

    if row is None:
        raise AssertionError("row must exist")
    if FakeSecret.DB in json.dumps(row[0], ensure_ascii=False):
        raise AssertionError("password leaked into the table")


async def test_foreign_key_cannot_read_rows(
    store: ConnectionStore, pool: AsyncPostgresPool
) -> None:
    connection_id = await store.add("main", _pg(FakeSecret.DB))

    foreign = ConnectionStore(_config(_key()), pool)
    with pytest.raises(SecretCryptoError):
        await foreign.get(connection_id)


async def test_get_unknown_raises(store: ConnectionStore) -> None:
    with pytest.raises(ConnectionNotFoundError):
        await store.get(10_000)


async def test_remove_drops_row_and_grants(store: ConnectionStore) -> None:
    connection_id = await store.add("main", _pg(FakeSecret.DB))
    await store.grant(connection_id, GrantTarget.user(1))

    if not await store.remove(connection_id):
        raise AssertionError("remove must report the dropped row")
    if await store.remove(connection_id):
        raise AssertionError("second remove must find nothing")

    with pytest.raises(ConnectionNotFoundError):
        await store.get(connection_id)

    if await store.grants_of(connection_id):
        raise AssertionError("grants must go with the row")


async def test_sync_roles_adds_missing_only(store: ConnectionStore) -> None:
    await store.sync_roles(["wrt", "read"])
    before = await store.roles()

    await store.sync_roles(["read", "admin"])
    after = await store.roles()

    if set(after) != {"wrt", "read", "admin"}:
        raise AssertionError("sync must add missing roles and keep existing ones")
    if after["read"] != before["read"]:
        raise AssertionError("existing role must keep its id")


async def test_grant_revoke_listing(store: ConnectionStore) -> None:
    await store.sync_roles(["read"])
    roles = await store.roles()
    connection_id = await store.add("main", _pg(FakeSecret.DB))

    await store.grant(connection_id, GrantTarget.user(5))
    await store.grant(connection_id, GrantTarget.role(roles["read"]))
    await store.grant(connection_id, GrantTarget.user(5))

    granted = list(await store.grants_of(connection_id))
    if granted != [GrantTarget.user(5), GrantTarget.role(roles["read"])]:
        raise AssertionError(f"unexpected grants: {granted}")

    if not await store.revoke(connection_id, GrantTarget.user(5)):
        raise AssertionError("revoke must report the dropped link")
    if await store.revoke(connection_id, GrantTarget.user(5)):
        raise AssertionError("second revoke must find nothing")

    if list(await store.grants_of(connection_id)) != [GrantTarget.role(roles["read"])]:
        raise AssertionError("role grant must stay")


async def test_for_subject_by_user_role_and_kind(store: ConnectionStore) -> None:
    await store.sync_roles(["read", "wrt"])
    roles = await store.roles()

    personal = await store.add("mine", _pg(FakeSecret.DB))
    shared = await store.add("shared", _pg(FakeSecret.DB_OTHER))
    other_role = await store.add("wrt-only", _pg(FakeSecret.DB))
    nobody = await store.add("nobody", _pg(FakeSecret.DB))
    web = await store.add("confl", _web(FakeSecret.HTTP_BEARER))
    ch = await store.add("ch", _ch())

    await store.grant(personal, GrantTarget.user(1))
    await store.grant(shared, GrantTarget.role(roles["read"]))
    await store.grant(other_role, GrantTarget.role(roles["wrt"]))
    await store.grant(web, GrantTarget.user(1))
    await store.grant(ch, GrantTarget.user(1))

    reader = Subject(user_id=1, roles=["read"])
    pg_rows = await store.for_subject(reader, ConnectionKind.POSTGRES)
    if [row.id for row in pg_rows] != [personal, shared]:
        raise AssertionError(f"reader must see personal and role rows: {pg_rows}")

    web_rows = await store.for_subject(reader, ConnectionKind.WEB)
    if [row.id for row in web_rows] != [web]:
        raise AssertionError("kind filter must hold")

    ch_rows = await store.for_subject(reader, ConnectionKind.CLICKHOUSE)
    if [row.id for row in ch_rows] != [ch]:
        raise AssertionError("clickhouse rows must be selectable")

    stranger = Subject(user_id=2, roles=[])
    if await store.for_subject(stranger, ConnectionKind.POSTGRES):
        raise AssertionError("stranger must see nothing")

    writer = Subject(user_id=2, roles=["wrt"])
    if [row.id for row in await store.for_subject(writer, ConnectionKind.POSTGRES)] != [
        other_role
    ]:
        raise AssertionError("role grant must be visible to any role holder")

    if nobody in [row.id for row in pg_rows]:
        raise AssertionError("ungranted row must stay invisible")


async def test_for_subject_lists_doubly_granted_row_once(
    store: ConnectionStore,
) -> None:
    await store.sync_roles(["read"])
    roles = await store.roles()
    connection_id = await store.add("main", _pg(FakeSecret.DB))
    await store.grant(connection_id, GrantTarget.user(1))
    await store.grant(connection_id, GrantTarget.role(roles["read"]))

    rows = await store.for_subject(
        Subject(user_id=1, roles=["read"]), ConnectionKind.POSTGRES
    )

    if [row.id for row in rows] != [connection_id]:
        raise AssertionError("row granted twice must be listed once")


async def test_revoke_takes_effect_immediately(store: ConnectionStore) -> None:
    connection_id = await store.add("main", _pg(FakeSecret.DB))
    await store.grant(connection_id, GrantTarget.user(1))
    subject = Subject(user_id=1, roles=[])

    if not await store.for_subject(subject, ConnectionKind.POSTGRES):
        raise AssertionError("granted row must be visible")

    await store.revoke(connection_id, GrantTarget.user(1))

    if await store.for_subject(subject, ConnectionKind.POSTGRES):
        raise AssertionError("revoked row must disappear without restart")
