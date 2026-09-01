"""ConnectionStore на реальном postgres: строки, гранты, выборка по субъекту."""

from __future__ import annotations

import base64
import json
import secrets as std_secrets
from uuid import UUID

import pytest
from psycopg import sql
from pydantic import SecretStr

from boba.connection_broker.store import ConnectionsConfig, ConnectionStore
from boba.connections.manifest import ConnectionTypes, UnknownConnectionKindError
from boba.connections.profile import (
    ConnectionNotFoundError,
    GrantKind,
    GrantTarget,
    StoredRole,
)
from boba.connections.secrets import SecretCryptoError
from boba.db.clickhouse.profile import (
    ClickHouseConfig,
    ClickHouseSettingsConfig,
    NoPasswordAuth,
)
from boba.db.postgres import AsyncPostgresPool
from boba.db.postgres.profile import (
    PasswordAuth,
    PostgresConfig,
    PostgresOptionsConfig,
    PostgresPoolConfig,
)
from boba.identity.context import Subject
from boba.stand.fakes import FakeSecret
from boba.transport.http.profile import BearerAuth, HttpProfile

pytestmark = pytest.mark.anyio

SCHEMA = "connections_test"


def _key() -> SecretStr:
    return SecretStr(base64.b64encode(std_secrets.token_bytes(32)).decode())


def _config(key: SecretStr) -> ConnectionsConfig:
    return ConnectionsConfig(enable=True, db_schema=SCHEMA, encryption_key=key)


def _pg(password: str) -> PostgresConfig:
    return PostgresConfig(
        host="db",
        dbname="n1bot",
        auth=PasswordAuth(method="password", user="boba", password=SecretStr(password)),
        options=PostgresOptionsConfig(),
        pool=PostgresPoolConfig(),
    )


def _ch() -> ClickHouseConfig:
    return ClickHouseConfig(
        host="ch",
        port=8123,
        interface="http",
        auth=NoPasswordAuth(method="no_password", user="boba"),
        settings=ClickHouseSettingsConfig(),
    )


def _web(token: str) -> HttpProfile:
    return HttpProfile(
        base_url="https://confl",
        auth=BearerAuth(method="bearer", token=SecretStr(token)),
    )


def _subject(user_id: UUID, roles: list[str]) -> Subject:
    return Subject(
        user_id=user_id, login=f"user-{user_id}", roles=frozenset(roles), profile="test"
    )


@pytest.fixture
async def store(pool: AsyncPostgresPool) -> ConnectionStore:
    async with pool.connection() as conn:
        await conn.execute(
            sql.SQL("drop schema if exists {} cascade").format(sql.Identifier(SCHEMA))
        )

    built = ConnectionStore(_config(_key()), ConnectionTypes.discover(), pool)
    await built.setup()
    return built


async def test_unknown_kind_is_skipped_in_lists_and_fails_on_get(
    store: ConnectionStore, pool: AsyncPostgresPool
) -> None:
    """Строка типа без установленного пакета: списки живут, точечный get падает."""
    key = _key()
    full = ConnectionStore(_config(key), ConnectionTypes.discover(), pool)
    await full.setup()
    connection_id = await full.add("legacy", _pg(FakeSecret.DB))

    # реестр без postgres: как будто пакет-владелец удалили
    stripped = ConnectionStore(_config(key), ConnectionTypes({}), pool)

    rows = await stripped.list_all()
    if [row.name for row in rows] != []:
        raise AssertionError(f"unknown kind must be skipped: {rows}")

    with pytest.raises(UnknownConnectionKindError, match="not installed"):
        await stripped.get(connection_id)


async def test_setup_is_idempotent(store: ConnectionStore) -> None:
    await store.setup()
    await store.setup()


async def test_add_and_get_restores_profile(store: ConnectionStore) -> None:
    connection_id = await store.add("main", _pg(FakeSecret.DB))

    stored = await store.get(connection_id)

    if stored.name != "main":
        raise AssertionError("name must survive the roundtrip")
    if stored.kind != "postgres":
        raise AssertionError("kind must follow the profile")
    if not isinstance(stored.profile, PostgresConfig):
        raise AssertionError("profile must come back as PostgresConfig")
    if not isinstance(stored.profile.auth, PasswordAuth):
        raise AssertionError(f"auth must survive: {stored.profile.auth}")
    if stored.profile.auth.password.get_secret_value() != FakeSecret.DB:
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
            sql.SQL("select data from {}").format(sql.Identifier(SCHEMA, "connections"))
        )
        row = await cur.fetchone()

    if row is None:
        raise AssertionError("row must exist")
    if FakeSecret.DB in json.dumps(row[0], ensure_ascii=False):
        raise AssertionError("password leaked into the table")


async def test_data_keeps_only_meaningful_fields(
    store: ConnectionStore, pool: AsyncPostgresPool
) -> None:
    """Дефолты в jsonb не едут: остаются дискриминатор и заданные поля."""
    await store.add("main", _ch())

    async with pool.cursor() as cur:
        await cur.execute(
            sql.SQL("select data from {}").format(sql.Identifier(SCHEMA, "connections"))
        )
        row = await cur.fetchone()

    if row is None:
        raise AssertionError("row must exist")

    data = row[0]
    if set(data) != {"kind", "host", "port", "interface", "auth"}:
        raise AssertionError(f"only meaningful fields expected: {sorted(data)}")
    if data["auth"] != {"method": "no_password", "user": "boba"}:
        raise AssertionError(f"auth must stay minimal: {data['auth']}")


async def test_kind_is_read_from_the_data(
    store: ConnectionStore, pool: AsyncPostgresPool
) -> None:
    """Отдельной колонки kind нет: вид соединения живёт в data->>'kind'."""
    connection_id = await store.add("main", _ch())

    async with pool.cursor() as cur:
        await cur.execute(
            sql.SQL("select data ->> 'kind' from {} where id = %(id)s").format(
                sql.Identifier(SCHEMA, "connections")
            ),
            {"id": connection_id},
        )
        row = await cur.fetchone()

    if row is None:
        raise AssertionError("row must exist")
    if row[0] != "clickhouse":
        raise AssertionError(f"kind must come from the profile: {row[0]}")


async def test_foreign_key_cannot_read_rows(
    store: ConnectionStore, pool: AsyncPostgresPool
) -> None:
    connection_id = await store.add("main", _pg(FakeSecret.DB))

    foreign = ConnectionStore(_config(_key()), ConnectionTypes.discover(), pool)
    with pytest.raises(SecretCryptoError):
        await foreign.get(connection_id)


async def test_get_unknown_raises(store: ConnectionStore) -> None:
    with pytest.raises(ConnectionNotFoundError):
        await store.get(UUID(int=10_000))


async def test_remove_drops_row_and_grants(store: ConnectionStore) -> None:
    connection_id = await store.add("main", _pg(FakeSecret.DB))
    await store.grant(connection_id, GrantTarget.user(UUID(int=1)))

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
    before = StoredRole.by_name(await store.roles())

    await store.sync_roles(["read", "admin"])
    after = StoredRole.by_name(await store.roles())

    if set(after) != {"wrt", "read", "admin"}:
        raise AssertionError("sync must add missing roles and keep existing ones")
    if after["read"] != before["read"]:
        raise AssertionError("existing role must keep its id")


async def test_grant_revoke_listing(store: ConnectionStore) -> None:
    await store.sync_roles(["read"])
    roles = StoredRole.by_name(await store.roles())
    connection_id = await store.add("main", _pg(FakeSecret.DB))

    await store.grant(connection_id, GrantTarget.user(UUID(int=5)))
    await store.grant(connection_id, GrantTarget.role(roles["read"]))
    await store.grant(connection_id, GrantTarget.user(UUID(int=5)))

    granted = {(t.kind, t.id) for t in await store.grants_of(connection_id)}
    if granted != {(GrantKind.USERS, UUID(int=5)), (GrantKind.ROLES, roles["read"])}:
        raise AssertionError(f"unexpected grants: {granted}")

    if not await store.revoke(connection_id, GrantTarget.user(UUID(int=5))):
        raise AssertionError("revoke must report the dropped link")
    if await store.revoke(connection_id, GrantTarget.user(UUID(int=5))):
        raise AssertionError("second revoke must find nothing")

    if list(await store.grants_of(connection_id)) != [GrantTarget.role(roles["read"])]:
        raise AssertionError("role grant must stay")


async def test_for_subject_by_user_role_and_kind(store: ConnectionStore) -> None:
    await store.sync_roles(["read", "wrt"])
    roles = StoredRole.by_name(await store.roles())

    personal = await store.add("mine", _pg(FakeSecret.DB))
    shared = await store.add("shared", _pg(FakeSecret.DB_OTHER))
    other_role = await store.add("wrt-only", _pg(FakeSecret.DB))
    nobody = await store.add("nobody", _pg(FakeSecret.DB))
    web = await store.add("confl", _web(FakeSecret.HTTP_BEARER))
    ch = await store.add("ch", _ch())

    await store.grant(personal, GrantTarget.user(UUID(int=1)))
    await store.grant(shared, GrantTarget.role(roles["read"]))
    await store.grant(other_role, GrantTarget.role(roles["wrt"]))
    await store.grant(web, GrantTarget.user(UUID(int=1)))
    await store.grant(ch, GrantTarget.user(UUID(int=1)))

    reader = _subject(UUID(int=1), ["read"])
    pg_rows = await store.for_subject(reader, "postgres")
    if {row.id for row in pg_rows} != {personal, shared}:
        raise AssertionError(f"reader must see personal and role rows: {pg_rows}")

    web_rows = await store.for_subject(reader, "web")
    if [row.id for row in web_rows] != [web]:
        raise AssertionError("kind filter must hold")

    ch_rows = await store.for_subject(reader, "clickhouse")
    if [row.id for row in ch_rows] != [ch]:
        raise AssertionError("clickhouse rows must be selectable")

    stranger = _subject(UUID(int=2), [])
    if await store.for_subject(stranger, "postgres"):
        raise AssertionError("stranger must see nothing")

    writer = _subject(UUID(int=2), ["wrt"])
    if [row.id for row in await store.for_subject(writer, "postgres")] != [other_role]:
        raise AssertionError("role grant must be visible to any role holder")

    if nobody in [row.id for row in pg_rows]:
        raise AssertionError("ungranted row must stay invisible")


async def test_for_subject_lists_doubly_granted_row_once(
    store: ConnectionStore,
) -> None:
    await store.sync_roles(["read"])
    roles = StoredRole.by_name(await store.roles())
    connection_id = await store.add("main", _pg(FakeSecret.DB))
    await store.grant(connection_id, GrantTarget.user(UUID(int=1)))
    await store.grant(connection_id, GrantTarget.role(roles["read"]))

    rows = await store.for_subject(_subject(UUID(int=1), ["read"]), "postgres")

    if [row.id for row in rows] != [connection_id]:
        raise AssertionError("row granted twice must be listed once")


async def test_revoke_takes_effect_immediately(store: ConnectionStore) -> None:
    connection_id = await store.add("main", _pg(FakeSecret.DB))
    await store.grant(connection_id, GrantTarget.user(UUID(int=1)))
    subject = _subject(UUID(int=1), [])

    if not await store.for_subject(subject, "postgres"):
        raise AssertionError("granted row must be visible")

    await store.revoke(connection_id, GrantTarget.user(UUID(int=1)))

    if await store.for_subject(subject, "postgres"):
        raise AssertionError("revoked row must disappear without restart")
