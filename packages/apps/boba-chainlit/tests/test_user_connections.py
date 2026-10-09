"""Соединения пользователя из таблицы доезжают до инструмента сервиса в песочнице.

Стенд: пользователь в users, его соединение в connections, грант в grants;
вызов идёт портом чата от имени этого пользователя на настоящий сервис
boba-mcp: чат запечатывает соединение, сервис открывает его и исполняет тело.
"""

from __future__ import annotations

import base64
import secrets as std_secrets
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from chainlit.user import PersistedUser
from chainlit.user import User as ChainlitUser
from chainlit_stand import ChatSessionStand, ServiceTools, SsoStand, ToolService
from psycopg import sql
from pydantic import SecretStr

from boba.auth.credentials import KerberosCredentialSource
from boba.chainlit.auth.kerberos import KerberosAuth
from boba.chainlit.data.data_layer import PostgresDataLayer
from boba.config import bind
from boba.connection_broker.store import ConnectionsConfig, ConnectionStore
from boba.connection_broker.user_connections import ArmedConnections
from boba.connections.manifest import ConnectionTypes
from boba.connections.marks import ConnectionRefusal
from boba.connections.sealed import ConnectionRef, ConnectionRefs
from boba.connections.stored import GrantTarget, StoredRole
from boba.db.postgres import AsyncPostgresPool
from boba.db.postgres.connection import PasswordAuth, PostgresConfig
from boba.identity.context import ContextKind
from boba.identity.errors import RefusalError
from boba.identity.session import UserMetadataField
from boba.kerberos import DelegatedAuth, DelegationMode, KeytabAuth
from boba.krb import KeytabCredentials
from boba.krb.seal import SsoTickets
from boba.messaging import MemoryMessageBus
from boba.runtime.refresh import BusRefreshSignal
from boba.stand.site import Stand
from boba.stand.toolsetup import Call
from boba.stand_core.context import CallStand
from boba.toolrun.hosted import DirectCalls
from boba.transport.http.connection import HttpConnection, NegotiateAuth, UrlScheme

SCHEMA = "connections_e2e"
ROLE = "analyst"
THREAD = "44444444-4444-4444-4444-444444444444"
PROFILE = "test"

pytestmark = [
    pytest.mark.integration,
    pytest.mark.anyio,
    pytest.mark.failif(bool(ToolService.missing()), reason=ToolService.missing()),
]


def _key() -> SecretStr:
    return SecretStr(base64.b64encode(std_secrets.token_bytes(32)).decode())


@pytest.fixture
async def store(pool: AsyncPostgresPool) -> ConnectionStore:
    async with pool.connection() as conn:
        await conn.execute(
            sql.SQL("drop schema if exists {} cascade").format(sql.Identifier(SCHEMA))
        )

    cfg = ConnectionsConfig(enable=True, db_schema=SCHEMA, encryption_key=_key())
    built = ConnectionStore(cfg, ConnectionTypes.discover(), pool)
    await built.setup()
    await built.sync_roles([ROLE])
    return built


@pytest.fixture
def service_pg(service_raw_config: Any) -> PostgresConfig:
    return bind(service_raw_config, path="postgres", model=PostgresConfig)


STAND = Stand.required()
SERVICE_PRINCIPAL = STAND.service_principal
SERVICE_USER = STAND.krb_pg_user
CH_URL = f"http://{STAND.ch_addr}:{STAND.ch_port}"


@pytest.fixture
def sso(tmp_path: Path) -> tuple[SsoTickets, str]:
    """Билет входа стенда: TGT сервисной учётки из keytab, запечатанный для JWT."""
    credentials = KeytabCredentials.of(
        KeytabAuth(
            method="kerberos_keytab",
            principal=SERVICE_PRINCIPAL,
            keytab=STAND.krb_pg_keytab,
        )
    )
    credentials.ensure()
    tickets = SsoStand.tickets(STAND.krb_config)
    sealed = SsoStand.sealed(
        tickets, SERVICE_PRINCIPAL, credentials.ccache, DelegationMode.FORWARDED, 3600
    )
    return tickets, sealed


@pytest.fixture
async def tools(
    tool_service: ToolService,
    call_stand: CallStand,
    store: ConnectionStore,
    sso: tuple[SsoTickets, str],
) -> AsyncIterator[ServiceTools]:
    """Инструменты сервиса портом чата: соединения пользователя берутся из
    таблицы теста, креды — из билета входа."""
    credentials = KerberosCredentialSource(
        sso[0],
        BusRefreshSignal(lambda: MemoryMessageBus("test"), call_stand.contexts),
    )
    connections = ArmedConnections(
        lambda: store, lambda: credentials, call_stand.contexts
    )
    opened = ServiceTools(tool_service, connections, call_stand.contexts, DirectCalls())
    await opened.start()
    try:
        yield opened
    finally:
        await opened.stop()


def _pg(name: str) -> str:
    return ConnectionRef(kind="postgres", name=name).render()


class Session:
    """Сессия chainlit от пользователя из таблицы users с его ролями."""

    @staticmethod
    async def user(layer: PostgresDataLayer, identifier: str) -> PersistedUser:
        roles = [ROLE]
        persisted = await layer.create_user(
            ChainlitUser(
                identifier=identifier,
                metadata={UserMetadataField.ROLES: roles},
            )
        )
        if persisted is None:
            raise AssertionError("user was not created")
        return persisted

    @staticmethod
    def sso_metadata(principal: str, sealed: str) -> dict[str, object]:
        """Метки JWT SSO-входа: провайдер, принципал и метка входа."""
        return {
            UserMetadataField.ROLES: [ROLE],
            UserMetadataField.PROVIDER: KerberosAuth.__name__,
            UserMetadataField.PRINCIPAL: principal,
            UserMetadataField.TICKET: sealed,
        }


async def test_granted_connection_is_visible_and_works(  # noqa: PLR0913 — фикстуры теста
    chat_session: ChatSessionStand,
    tools: ServiceTools,
    catalog: Any,
    store: ConnectionStore,
    layer: PostgresDataLayer,
    service_pg: PostgresConfig,
) -> None:
    user = await Session.user(layer, "conn-owner")
    connection_id = await store.add("main", service_pg)
    await store.grant(connection_id, GrantTarget.user(UUID(user.id)))
    chat_session.sign_in(user, user.metadata, THREAD, PROFILE)

    targets = await Call.ok(catalog)
    names = [ConnectionRefs().parse(row["connection"]).name for row in targets.rows]
    if names != ["main"]:
        raise AssertionError(f"whitelist must hold the granted row only: {names}")

    result = await Call.ok(
        tools["pg_query"],
        connection=_pg("main"),
        sql="select 1 as answer",
        offset=0,
        limit=50,
    )
    if result.statements[0].rows != [{"answer": 1}]:
        raise AssertionError(f"query must run on the granted connection: {result}")


async def test_role_grant_reaches_every_role_holder(  # noqa: PLR0913 — фикстуры теста
    chat_session: ChatSessionStand,
    tools: ServiceTools,
    catalog: Any,
    store: ConnectionStore,
    layer: PostgresDataLayer,
    service_pg: PostgresConfig,
) -> None:
    user = await Session.user(layer, "conn-role-holder")
    roles = StoredRole.by_name(await store.roles())
    connection_id = await store.add("shared", service_pg)
    await store.grant(connection_id, GrantTarget.role(roles[ROLE]))
    chat_session.sign_in(user, user.metadata, THREAD, PROFILE)

    targets = await Call.ok(catalog)
    names = [ConnectionRefs().parse(row["connection"]).name for row in targets.rows]
    if names != ["shared"]:
        raise AssertionError(f"role grant must be visible: {names}")


async def test_stranger_sees_nothing(  # noqa: PLR0913 — фикстуры теста
    chat_session: ChatSessionStand,
    tools: ServiceTools,
    catalog: Any,
    store: ConnectionStore,
    layer: PostgresDataLayer,
    service_pg: PostgresConfig,
) -> None:
    owner = await Session.user(layer, "conn-owner-2")
    stranger = await Session.user(layer, "conn-stranger")
    connection_id = await store.add("main", service_pg)
    await store.grant(connection_id, GrantTarget.user(UUID(owner.id)))
    chat_session.sign_in(stranger, stranger.metadata, THREAD, PROFILE)

    targets = await Call.ok(catalog)
    if targets.rows:
        raise AssertionError(f"stranger must see no connections: {targets.rows}")

    refused = await Call.result(
        tools["pg_query"], connection=_pg("main"), sql="select 1", offset=0, limit=50
    )

    if refused.error_kind != ConnectionRefusal.NOT_VISIBLE:
        raise AssertionError(f"unexpected refusal: {refused}")


async def test_revoke_applies_to_the_next_call(  # noqa: PLR0913 — фикстуры теста
    chat_session: ChatSessionStand,
    tools: ServiceTools,
    catalog: Any,
    store: ConnectionStore,
    layer: PostgresDataLayer,
    service_pg: PostgresConfig,
) -> None:
    user = await Session.user(layer, "conn-revoked")
    connection_id = await store.add("main", service_pg)
    target = GrantTarget.user(UUID(user.id))
    await store.grant(connection_id, target)
    chat_session.sign_in(user, user.metadata, THREAD, PROFILE)

    before = await Call.ok(catalog)
    if not before.rows:
        raise AssertionError("granted row must be visible before revoke")

    await store.revoke(connection_id, target)

    after = await Call.ok(catalog)
    if after.rows:
        raise AssertionError("revoked row must disappear without a restart")


async def test_ambiguous_name_is_refused(  # noqa: PLR0913 — фикстуры теста
    chat_session: ChatSessionStand,
    tools: ServiceTools,
    catalog: Any,
    store: ConnectionStore,
    layer: PostgresDataLayer,
    service_pg: PostgresConfig,
) -> None:
    user = await Session.user(layer, "conn-ambiguous")
    first = await store.add("main", service_pg)
    second = await store.add("main", service_pg)
    await store.grant(first, GrantTarget.user(UUID(user.id)))
    await store.grant(second, GrantTarget.user(UUID(user.id)))
    chat_session.sign_in(user, user.metadata, THREAD, PROFILE)

    targets = await Call.ok(catalog)
    if targets.rows:
        raise AssertionError(f"ambiguous name must not be listed: {targets.rows}")

    refused = await Call.result(
        tools["pg_query"], connection=_pg("main"), sql="select 1", offset=0, limit=50
    )

    if refused.error_kind != ConnectionRefusal.AMBIGUOUS:
        raise AssertionError(f"unexpected refusal: {refused}")


async def test_delegated_connection_runs_as_the_session_principal(  # noqa: PLR0913 — фикстуры теста
    chat_session: ChatSessionStand,
    sso: tuple[SsoTickets, str],
    tools: ServiceTools,
    store: ConnectionStore,
    layer: PostgresDataLayer,
    service_pg: PostgresConfig,
) -> None:
    """Строка без ключей: в базу в песочнице идёт билет делегированного входа."""
    user = await Session.user(layer, "conn-delegated")
    delegated = service_pg.model_copy(
        update={"auth": DelegatedAuth(method="kerberos_delegated")}
    )
    connection_id = await store.add("mine", delegated)
    await store.grant(connection_id, GrantTarget.user(UUID(user.id)))
    chat_session.sign_in(
        user, Session.sso_metadata(SERVICE_PRINCIPAL, sso[1]), THREAD, PROFILE
    )

    result = await Call.ok(
        tools["pg_query"],
        connection=_pg("mine"),
        sql="select current_user as who",
        offset=0,
        limit=50,
    )
    if result.statements[0].rows != [{"who": SERVICE_USER}]:
        raise AssertionError(f"query must run as the delegated principal: {result}")


async def test_delegated_connection_refuses_local_login(
    chat_session: ChatSessionStand,
    tools: ServiceTools,
    store: ConnectionStore,
    layer: PostgresDataLayer,
    service_pg: PostgresConfig,
) -> None:
    user = await Session.user(layer, "conn-delegated-local")
    delegated = service_pg.model_copy(
        update={"auth": DelegatedAuth(method="kerberos_delegated")}
    )
    connection_id = await store.add("mine", delegated)
    await store.grant(connection_id, GrantTarget.user(UUID(user.id)))
    chat_session.sign_in(user, user.metadata, THREAD, PROFILE)

    refused = await Call.result(
        tools["pg_query"], connection=_pg("mine"), sql="select 1", offset=0, limit=50
    )

    if refused.error_kind != ConnectionRefusal.NO_DELEGATION:
        raise AssertionError(f"unexpected refusal: {refused}")


async def test_unreachable_database_is_reported_by_the_body(
    chat_session: ChatSessionStand,
    tools: ServiceTools,
    store: ConnectionStore,
    layer: PostgresDataLayer,
    service_pg: PostgresConfig,
) -> None:
    """Соединение выдано, но база за ним не отвечает: отказ из песочницы."""
    user = await Session.user(layer, "conn-dead-db")
    dead = service_pg.model_copy(
        update={
            "hostaddr": "127.0.0.1",
            "port": 1,
            "auth": PasswordAuth(
                method="password", user="boba", password=SecretStr("none")
            ),
            "connect_timeout": 2,
        }
    )
    connection_id = await store.add("dead", dead)
    await store.grant(connection_id, GrantTarget.user(UUID(user.id)))
    chat_session.sign_in(user, user.metadata, THREAD, PROFILE)

    refused = await Call.result(
        tools["pg_query"], connection=_pg("dead"), sql="select 1", offset=0, limit=50
    )

    if refused.error_kind != "PostgresError":
        raise AssertionError(f"unexpected failure: {refused}")


@pytest.mark.failif(
    not STAND.ch_addr, reason="в конфиге стенда нет clickhouse (ch_addr)"
)
async def test_web_negotiate_connection_authenticates_as_the_principal(
    chat_session: ChatSessionStand,
    sso: tuple[SsoTickets, str],
    tools: ServiceTools,
    store: ConnectionStore,
    layer: PostgresDataLayer,
) -> None:
    """Web-строка negotiate/delegated: HTTP-интерфейс ClickHouse видит принципал."""
    user = await Session.user(layer, "conn-web-negotiate")
    row = HttpConnection(
        scheme=UrlScheme.HTTP,
        host=STAND.ch_addr,
        port=STAND.ch_port,
        ssl_verify=False,
        auth=NegotiateAuth(
            method="negotiate",
            kerberos=DelegatedAuth(method="kerberos_delegated"),
            service_host=STAND.ch_host,
        ),
    )
    connection_id = await store.add("ch-http", row)
    await store.grant(connection_id, GrantTarget.user(UUID(user.id)))
    chat_session.sign_in(
        user, Session.sso_metadata(SERVICE_PRINCIPAL, sso[1]), THREAD, PROFILE
    )

    result = await Call.ok(
        tools["web_fetch_page"],
        url=f"{CH_URL}/?query=select%20currentUser()",
        connection=ConnectionRef(kind="web", name="ch-http").render(),
        as_markdown=False,
        line_offset=0,
        line_count=5,
    )
    if SERVICE_USER not in result.text:
        raise AssertionError(f"clickhouse must see the principal: {result.text}")


async def test_call_outside_session_is_refused(
    call_stand: CallStand, catalog: Any
) -> None:
    from chainlit.context import init_http_context

    init_http_context(user=None)
    call_stand.clear()

    with pytest.raises(RefusalError) as caught:
        await Call.result(catalog)

    if caught.value.kind != ContextKind.NO_CONTEXT:
        raise AssertionError(f"unexpected refusal kind: {caught.value.kind}")
