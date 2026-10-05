"""Вызовы инструментов сервиса на делегированном kerberos: pg, ch и web в песочнице.

Полный боевой путь: SPNEGO-accept собирает evidence-креды входа (режим
constrained, как в конфиге), обвязка соединений выпускает по ним билет к SPN
строки и запечатывает его в соединении, сервис boba-mcp открывает соединение,
и тело инструмента работает этим билетом внутри песочницы. Каждый тест
спрашивает у самого сервиса, кем он видит клиента.

Стенд: живой KDC, postgres и clickhouse домена; web-инструмент ходит в
HTTP-интерфейс того же clickhouse по Negotiate. Учётка приложения заведена в
обоих сервисах и значится в msDS-AllowedToDelegateTo.
"""

from __future__ import annotations

import base64
import secrets as std_secrets
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from uuid import UUID

import krb5
import pytest
from chainlit.user import PersistedUser
from chainlit.user import User as ChainlitUser
from chainlit_stand import ChatSessionStand, ServiceTools, SsoStand, ToolService
from gssapi import Credentials, Name, NameType, SecurityContext
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
from boba.connections.stored import ConnectionBase, GrantTarget
from boba.db.clickhouse.connection import ClickHouseConfig
from boba.db.postgres import AsyncPostgresPool
from boba.db.postgres.connection import PostgresConfig
from boba.identity.session import UserMetadataField
from boba.kerberos import (
    AcceptConfig,
    ConstrainedDelegation,
    DelegatedAuth,
)
from boba.krb import SpnegoAcceptor, TicketCapture
from boba.krb.seal import SsoTickets
from boba.messaging import MemoryMessageBus
from boba.runtime.refresh import BusRefreshSignal
from boba.stand.site import Stand
from boba.stand.toolsetup import Call
from boba.stand_core.context import CallStand
from boba.transport.http.connection import HttpConnection, NegotiateAuth, UrlScheme

STAND = Stand.required()
KRB5_CONF = Path(STAND.krb_config)
SERVICE_KEYTAB = Path(STAND.krb_http_keytab)
SERVICE_SPN = f"HTTP/{STAND.krb_domain}@{STAND.krb_realm}"
PRINCIPAL = STAND.reader_principal
ROLE_NAME = PRINCIPAL.split("@")[0]
"""Как принципал выглядит для сервисов: роль postgres и пользователь ch.

Клиентом входа выступает обычный пользователь домена: у сервисной учётки
accept и initiate совпадают, и evidence-креды KDC для неё не выдаёт.
"""

SCHEMA = "delegated_tools"
ROLE = "analyst"
THREAD = "44444444-4444-4444-4444-444444444444"
PROFILE = "test"


pytestmark = [
    pytest.mark.integration,
    pytest.mark.anyio,
    pytest.mark.skipif(bool(ToolService.missing()), reason=ToolService.missing()),
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


class Browser:
    """Клиентская сторона SSO: AP-REQ к SPN сервиса без форварда TGT."""

    @staticmethod
    def token(tmp_path: Path, password: str) -> bytes:
        context = krb5.init_context()
        principal = krb5.parse_name_flags(context, PRINCIPAL.encode())
        options = krb5.get_init_creds_opt_alloc(context)
        krb5.get_init_creds_opt_set_forwardable(options, True)
        secret = password.encode()
        tgt = krb5.get_init_creds_password(context, principal, options, secret)

        ccache = f"FILE:{tmp_path / 'browser'}"
        cache = krb5.cc_resolve(context, ccache.encode())
        krb5.cc_initialize(context, cache, principal)
        krb5.cc_store_cred(context, cache, tgt)

        creds = Credentials(usage="initiate", store={b"ccache": ccache.encode()})
        target = Name(SERVICE_SPN, NameType.kerberos_principal)
        initiator = SecurityContext(name=target, creds=creds, usage="initiate", flags=0)
        return initiator.step()


@pytest.fixture
def user_password(service_raw_config: Any) -> str:
    """Пароль тестового пользователя домена; браузер им получает свой TGT."""
    from omegaconf import OmegaConf

    return str(OmegaConf.select(service_raw_config, "site.ldap_bind_password"))


@pytest.fixture
async def sso_login(tmp_path: Path, user_password: str) -> tuple[SsoTickets, str]:
    """Вход по SPNEGO: открыватель билетов и запечатанный билет этого входа.

    accept идёт асинхронно, как в приложении: синхронный захват лока
    KerberosEnv в потоке цикла встал бы за воркером пула, держащим лок
    через await connect."""
    delegation = ConstrainedDelegation(
        service_ccache=f"FILE:{tmp_path / 'service'}",
        krb5_config=str(KRB5_CONF),
    )
    accept = AcceptConfig(service_name=SERVICE_SPN, keytab=str(SERVICE_KEYTAB))
    token = Browser.token(tmp_path, user_password)
    identity = await SpnegoAcceptor(accept, delegation).accept_async(token)
    ticket = TicketCapture(delegation).capture(identity)
    if ticket is None:
        raise AssertionError("constrained sign-in captured no evidence credentials")

    tickets = SsoStand.tickets(str(KRB5_CONF))
    return tickets, tickets.sealer.seal(ticket)


@pytest.fixture
def tickets(sso_login: tuple[SsoTickets, str]) -> SsoTickets:
    return sso_login[0]


@pytest.fixture
async def session(
    chat_session: ChatSessionStand,
    layer: PostgresDataLayer,
    sso_login: tuple[SsoTickets, str],
) -> PersistedUser:
    """Пользователь чата, вошедший этим SSO-входом: метки лежат в JWT сессии."""
    metadata: dict[str, object] = {
        UserMetadataField.ROLES: [ROLE],
        UserMetadataField.PROVIDER: KerberosAuth.__name__,
        UserMetadataField.PRINCIPAL: PRINCIPAL,
        UserMetadataField.TICKET: sso_login[1],
    }
    user = await layer.create_user(
        ChainlitUser(identifier="delegated-tools", metadata=metadata)
    )
    if user is None:
        raise AssertionError("user was not created")

    chat_session.sign_in(user, metadata, THREAD, PROFILE)
    return user


@pytest.fixture
async def tools(
    tool_service: ToolService,
    call_stand: CallStand,
    store: ConnectionStore,
    tickets: SsoTickets,
) -> AsyncIterator[ServiceTools]:
    """Инструменты сервиса портом чата: соединения пользователя берутся из
    таблицы теста, креды — из evidence-билета его входа."""
    credentials = KerberosCredentialSource(
        tickets,
        BusRefreshSignal(lambda: MemoryMessageBus("test"), call_stand.contexts),
    )
    connections = ArmedConnections(
        lambda: store, lambda: credentials, call_stand.contexts
    )
    opened = ServiceTools(tool_service, connections, call_stand.contexts)
    await opened.start()
    try:
        yield opened
    finally:
        await opened.stop()


def _ref(kind: str, name: str) -> str:
    return ConnectionRef(kind=kind, name=name).render()


def _delegated() -> DelegatedAuth:
    """Строка «идёт сам пользователь»: креды даёт его вход в приложение."""
    return DelegatedAuth(method="kerberos_delegated")


async def _granted(
    store: ConnectionStore,
    session: PersistedUser,
    name: str,
    connection: ConnectionBase,
) -> None:
    connection_id = await store.add(name, connection)
    await store.grant(connection_id, GrantTarget.user(UUID(session.id)))


@pytest.fixture
def delegated_pg(service_raw_config: Any) -> PostgresConfig:
    service = bind(service_raw_config, path="postgres", model=PostgresConfig)
    return service.model_copy(update={"auth": _delegated()})


@pytest.fixture
def delegated_ch(service_raw_config: Any) -> ClickHouseConfig:
    service = bind(service_raw_config, path="clickhouse", model=ClickHouseConfig)
    return service.model_copy(update={"auth": _delegated()})


@pytest.fixture
def delegated_web(delegated_ch: ClickHouseConfig) -> HttpConnection:
    """HTTP-интерфейс того же clickhouse, что у ch_query, по Negotiate: на
    currentUser() он называет принципала запроса."""
    host = delegated_ch.host
    if host is None:
        raise AssertionError("section [clickhouse] of the stand has no host")

    port = delegated_ch.port
    if port is None:
        raise AssertionError("section [clickhouse] of the stand has no port")

    interface = delegated_ch.interface
    if interface is None:
        raise AssertionError("section [clickhouse] of the stand has no interface")

    return HttpConnection(
        scheme=UrlScheme(interface),
        host=host,
        port=port,
        ssl_verify=False,
        timeout_sec=30.0,
        auth=NegotiateAuth(
            method="negotiate",
            kerberos=DelegatedAuth(method="kerberos_delegated"),
        ),
    )


async def test_postgres_query_runs_as_the_signed_in_principal(
    tools: ServiceTools,
    store: ConnectionStore,
    session: PersistedUser,
    delegated_pg: PostgresConfig,
) -> None:
    await _granted(store, session, "pg-me", delegated_pg)

    result = await Call.ok(
        tools["pg_query"],
        connection=_ref("postgres", "pg-me"),
        sql="select current_user as who",
        offset=0,
        limit=50,
    )

    rows = result.statements[0].rows
    if rows != [{"who": ROLE_NAME}]:
        raise AssertionError(f"postgres must see the principal: {rows}")


async def test_clickhouse_query_runs_as_the_signed_in_principal(
    tools: ServiceTools,
    store: ConnectionStore,
    session: PersistedUser,
    delegated_ch: ClickHouseConfig,
) -> None:
    await _granted(store, session, "ch-me", delegated_ch)

    result = await Call.ok(
        tools["ch_query"],
        connection=_ref("clickhouse", "ch-me"),
        sql="select currentUser() as who",
        offset=0,
        limit=50,
    )

    rows = result.statements[0].rows
    if rows != [{"who": ROLE_NAME}]:
        raise AssertionError(f"clickhouse must see the principal: {rows}")


async def test_web_page_is_fetched_as_the_signed_in_principal(
    tools: ServiceTools,
    store: ConnectionStore,
    session: PersistedUser,
    delegated_web: HttpConnection,
) -> None:
    await _granted(store, session, "ch-http", delegated_web)

    url = delegated_web.url_of("/").copy_merge_params({"query": "select currentUser()"})
    result = await Call.ok(
        tools["web_fetch_page"],
        url=str(url),
        connection=_ref("web", "ch-http"),
        as_markdown=False,
        line_offset=0,
        line_count=5,
    )

    if ROLE_NAME not in result.text.splitlines():
        raise AssertionError(f"clickhouse must see the principal: {result.text!r}")


async def test_targets_list_only_granted_connections(  # noqa: PLR0913 — три вида сразу
    tools: ServiceTools,
    catalog: Any,
    store: ConnectionStore,
    session: PersistedUser,
    delegated_pg: PostgresConfig,
    delegated_ch: ClickHouseConfig,
    delegated_web: HttpConnection,
) -> None:
    """Каждый инструмент видит соединения своего вида и только их."""
    await _granted(store, session, "pg-me", delegated_pg)
    await _granted(store, session, "ch-me", delegated_ch)
    await _granted(store, session, "ch-http", delegated_web)

    listed = await Call.ok(catalog)
    by_kind: dict[str, list[str]] = {}
    for row in listed.rows:
        name = ConnectionRefs().parse(str(row["connection"])).name
        by_kind.setdefault(str(row["kind"]), []).append(name)

    if by_kind.get("postgres") != ["pg-me"]:
        raise AssertionError(f"pg targets: {listed.rows}")
    if by_kind.get("clickhouse") != ["ch-me"]:
        raise AssertionError(f"ch targets: {listed.rows}")
    if by_kind.get("web") != ["ch-http"]:
        raise AssertionError(f"web targets: {listed.rows}")


async def test_revoked_connection_stops_working_at_once(
    tools: ServiceTools,
    store: ConnectionStore,
    session: PersistedUser,
    delegated_pg: PostgresConfig,
) -> None:
    connection_id = await store.add("pg-me", delegated_pg)
    target = GrantTarget.user(UUID(session.id))
    await store.grant(connection_id, target)

    await Call.ok(
        tools["pg_query"],
        connection=_ref("postgres", "pg-me"),
        sql="select 1 as one",
        offset=0,
        limit=50,
    )

    await store.revoke(connection_id, target)

    refused = await Call.result(
        tools["pg_query"],
        connection=_ref("postgres", "pg-me"),
        sql="select 1 as one",
        offset=0,
        limit=50,
    )

    if refused.error_kind != ConnectionRefusal.NOT_VISIBLE:
        raise AssertionError(f"unexpected refusal: {refused}")
