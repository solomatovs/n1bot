"""connection_list и connection_search на живой базе: строки connections/roles/grants
и субъект.

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
from boba.connections.grants import ConnectionFilter
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
from boba.transport.http.profile import HttpConnection

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

    if result.rows[0][CatalogColumn.HOST] != "db.example":
        raise AssertionError(f"host comes from the profile: {result.rows[0]}")


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


@pytest.fixture
async def granted(store: ConnectionStore) -> Subject:
    """Субъект с четырьмя соединениями трёх видов: по ним ищут тесты поиска."""
    subject = _subject(frozenset({ROLE}))
    role_id = (await store.roles())[0].id

    sales = await store.add("pg-sales", _pg("Хранилище продаж, витрины", "dwh01.corp"))
    await store.grant(sales, GrantTarget.user(subject.user_id))

    crm = await store.add("pg-crm", _pg("CRM production, sales pipeline", "crm.corp"))
    await store.grant(crm, GrantTarget.role(role_id))

    wiki = await store.add(
        "wiki",
        HttpConnection(
            host="wiki.corp", port=443, description="Confluence with sales docs"
        ),
    )
    await store.grant(wiki, GrantTarget.user(subject.user_id))

    hidden = await store.add("pg-hidden", _pg("sales, but not granted", "dwh01.corp"))
    await store.grant(hidden, GrantTarget.user(uuid4()))

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

    if web.rows[0][CatalogColumn.HOST] != "wiki.corp":
        raise AssertionError(f"web host comes from the profile: {web.rows[0]}")


async def test_search_without_filters_equals_list(
    granted: Subject, catalog: GrantedConnections
) -> None:
    listed = await catalog.rows(granted)
    searched = await catalog.search(granted, ConnectionFilter.none())

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
    await store.grant(percent, GrantTarget.user(subject.user_id))

    plain = await store.add("plain", _pg("100 percent"))
    await store.grant(plain, GrantTarget.user(subject.user_id))

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
    await store.grant(personal, GrantTarget.user(subject.user_id))

    by_role = await store.add("main", _pg("crm replica"))
    await store.grant(by_role, GrantTarget.role(role_id))

    result = await catalog.search(subject, ConnectionFilter(description="sales"))

    if result.rows != []:
        raise AssertionError(f"ambiguous name must stay hidden: {result.rows}")
