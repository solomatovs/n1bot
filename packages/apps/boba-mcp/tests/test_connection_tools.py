"""connection_list и connection_search на живой базе: строки connections/roles/grants
и субъект.

Строки и гранты кладутся хранилищем брокера (как их положит приложение),
выборка идёт каталогом инструментов через то же хранилище.
"""

from __future__ import annotations

import secrets
from collections.abc import Mapping, Sequence
from uuid import UUID, uuid4

import pytest
from psycopg import sql
from pydantic import SecretStr

from boba.access.grants import ConnectionFilter
from boba.connections.manifest import ConnectionTypes
from boba.connections.stored import GrantTarget
from boba.crypto import SecretCipher
from boba.db.postgres import AsyncPostgresPool
from boba.db.postgres.connection import (
    PasswordAuth,
    PostgresConfig,
    PostgresOptionsConfig,
    PostgresPoolConfig,
)
from boba.identity.context import Subject
from boba.identity.principal import UserIdentity
from boba.identity.session import Login
from boba.mcp_server.connections.store import ConnectionStore
from boba.mcp_server.connections.tools import GrantedConnections
from boba.stand.migrate import StandMigrations
from boba.stand.roles import RoleSeed
from boba.transport.http.connection import HttpConnection

pytestmark = [pytest.mark.anyio, pytest.mark.integration]

SCHEMA = "connections_tool"
ROLE = "analyst"


@pytest.fixture
async def store(
    pool: AsyncPostgresPool, test_postgres: PostgresConfig
) -> ConnectionStore:
    async with pool.connection() as conn:
        await conn.execute(
            sql.SQL("drop schema if exists {} cascade").format(sql.Identifier(SCHEMA))
        )

    cipher = SecretCipher(secrets.token_bytes(32))
    await StandMigrations().application(test_postgres, SCHEMA, "boba.mcp_server")

    built = ConnectionStore(pool, SCHEMA, cipher, ConnectionTypes.discover())
    await RoleSeed(built).add([ROLE])
    return built


@pytest.fixture
def catalog(store: ConnectionStore) -> GrantedConnections:
    return GrantedConnections(store)


def _subject(
    roles: frozenset[str] = frozenset(), login: str = "tester"
) -> Subject:
    identity = UserIdentity(realm="test", login=Login(login))
    return Subject.of_user(uuid4(), identity, roles, "test")


async def _grant_user(
    store: ConnectionStore, connection_id: UUID, subject: Subject
) -> None:
    await RoleSeed(store).user(subject.user_id, subject.identity, subject.roles)
    await store.grant(connection_id, GrantTarget.user(subject.user_id))


def _pg(description: str, host: str = "db.example") -> PostgresConfig:
    return PostgresConfig(
        host=host,
        dbname="app",
        description=description,
        auth=PasswordAuth(method="password", user="app", password=SecretStr("x")),
        options=PostgresOptionsConfig(),
        pool=PostgresPoolConfig(),
    )


def _names(rows: Sequence[Mapping[str, object]]) -> list[object]:
    names: list[object] = []
    for row in rows:
        names.append(row["name"])

    return names


async def test_personal_and_role_grants_are_listed(
    store: ConnectionStore, catalog: GrantedConnections
) -> None:
    subject = _subject(frozenset({ROLE}))
    role_id = (await store.roles())[0].id

    mine = await store.add("mine", _pg("personal"))
    await _grant_user(store, mine, subject)

    shared = await store.add("shared", _pg("by role"))
    await store.grant(shared, GrantTarget.role(role_id))

    stranger = await store.add("stranger", _pg("someone else's"))
    await _grant_user(store, stranger, _subject(login="stranger"))

    result = await catalog.rows(subject)

    if _names(result.rows) != ["mine", "shared"]:
        raise AssertionError(f"granted rows only, sorted by name: {result.rows}")

    if result.rows[0]["kind"] != "postgres":
        raise AssertionError(f"kind comes from the connection: {result.rows[0]}")

    if result.rows[1]["description"] != "by role":
        raise AssertionError(f"description comes from the connection: {result.rows[1]}")

    if result.rows[0]["host"] != "db.example":
        raise AssertionError(f"host comes from the connection: {result.rows[0]}")

    if result.rows[0]["url"] != "postgresql://db.example:5432/app":
        raise AssertionError(f"url is built by the type package: {result.rows[0]}")


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
    await _grant_user(store, personal, subject)

    by_role = await store.add("main", _pg("by role"))
    await store.grant(by_role, GrantTarget.role(role_id))

    other = await store.add("other", _pg("unique"))
    await _grant_user(store, other, subject)

    result = await catalog.rows(subject)

    if _names(result.rows) != ["other"]:
        raise AssertionError(f"ambiguous name must not be listed: {result.rows}")


@pytest.fixture
async def granted(store: ConnectionStore) -> Subject:
    """Субъект с четырьмя соединениями трёх видов: по ним ищут тесты поиска."""
    subject = _subject(frozenset({ROLE}))
    role_id = (await store.roles())[0].id

    sales = await store.add("pg-sales", _pg("Хранилище продаж, витрины", "dwh01.corp"))
    await _grant_user(store, sales, subject)

    crm = await store.add("pg-crm", _pg("CRM production, sales pipeline", "crm.corp"))
    await store.grant(crm, GrantTarget.role(role_id))

    wiki = await store.add(
        "wiki",
        HttpConnection(
            host="wiki.corp", port=443, description="Confluence with sales docs"
        ),
    )
    await _grant_user(store, wiki, subject)

    hidden = await store.add("pg-hidden", _pg("sales, but not granted", "dwh01.corp"))
    await _grant_user(store, hidden, _subject(login="hidden"))

    return subject


async def test_search_by_kind_is_exact_and_case_insensitive(
    granted: Subject, catalog: GrantedConnections
) -> None:
    result = await catalog.search(granted, ConnectionFilter(kind="Postgres"))

    if _names(result.rows) != ["pg-crm", "pg-sales"]:
        raise AssertionError(f"kind filter: {result.rows}")


async def test_search_by_host_fragment(
    granted: Subject, catalog: GrantedConnections
) -> None:
    result = await catalog.search(granted, ConnectionFilter(host="DWH01"))

    if _names(result.rows) != ["pg-sales"]:
        raise AssertionError(f"host substring filter: {result.rows}")


async def test_search_by_description_words_needs_every_word(
    granted: Subject, catalog: GrantedConnections
) -> None:
    both = await catalog.search(granted, ConnectionFilter(description="SALES pipeline"))

    if _names(both.rows) != ["pg-crm"]:
        raise AssertionError(f"every word must match: {both.rows}")

    russian = await catalog.search(granted, ConnectionFilter(description="продаж"))

    if _names(russian.rows) != ["pg-sales"]:
        raise AssertionError(f"substring matches a word form: {russian.rows}")


async def test_search_filters_combine_and_skip_foreign_rows(
    granted: Subject, catalog: GrantedConnections
) -> None:
    result = await catalog.search(
        granted, ConnectionFilter(kind="postgres", name="pg-", host=".corp")
    )

    if _names(result.rows) != ["pg-crm", "pg-sales"]:
        raise AssertionError(
            f"filters combine with AND, foreign rows hidden: {result.rows}"
        )

    web = await catalog.search(
        granted, ConnectionFilter(kind="web", description="sales")
    )

    if _names(web.rows) != ["wiki"]:
        raise AssertionError(f"web row carries its host and description: {web.rows}")

    if web.rows[0]["host"] != "wiki.corp":
        raise AssertionError(f"web host comes from the connection: {web.rows[0]}")
    if web.rows[0]["url"] != "https://wiki.corp":
        raise AssertionError(f"web url is the service root: {web.rows[0]}")


async def test_search_without_filters_equals_list(
    granted: Subject, catalog: GrantedConnections
) -> None:
    listed = await catalog.rows(granted)
    searched = await catalog.search(granted, ConnectionFilter())

    if listed != searched:
        raise AssertionError(f"empty filter must equal the list: {searched}")


async def test_search_without_matches_says_so(
    granted: Subject, catalog: GrantedConnections
) -> None:
    result = await catalog.search(granted, ConnectionFilter(host="nowhere"))

    if result.rows != []:
        raise AssertionError(f"no match, no rows: {result.rows}")

    if result.note != GrantedConnections.NO_MATCH_NOTE:
        raise AssertionError(f"empty search carries its own note: {result.note!r}")


async def test_like_specials_in_filters_are_literal(
    store: ConnectionStore, catalog: GrantedConnections
) -> None:
    subject = _subject()
    percent = await store.add("pct", _pg("100% coverage"))
    await _grant_user(store, percent, subject)

    plain = await store.add("plain", _pg("100 percent"))
    await _grant_user(store, plain, subject)

    result = await catalog.search(subject, ConnectionFilter(description="100%"))

    if _names(result.rows) != ["pct"]:
        raise AssertionError(
            f"percent must be a literal, not a wildcard: {result.rows}"
        )


async def test_duplicate_name_stays_hidden_when_a_filter_drops_its_twin(
    store: ConnectionStore, catalog: GrantedConnections
) -> None:
    """Дубль считается по всем строкам субъекта: фильтр не делает имя уникальным,
    вызов по нему всё равно неоднозначен."""
    subject = _subject(frozenset({ROLE}))
    role_id = (await store.roles())[0].id

    personal = await store.add("main", _pg("sales warehouse"))
    await _grant_user(store, personal, subject)

    by_role = await store.add("main", _pg("crm replica"))
    await store.grant(by_role, GrantTarget.role(role_id))

    result = await catalog.search(subject, ConnectionFilter(description="sales"))

    if result.rows != []:
        raise AssertionError(f"ambiguous name must stay hidden: {result.rows}")
