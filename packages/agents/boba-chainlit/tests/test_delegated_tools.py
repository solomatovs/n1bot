"""Вызовы инструментов на делегированном kerberos: pg, ch и web в песочнице.

Полный боевой путь: SPNEGO-accept собирает evidence-креды входа (режим
constrained, как в конфиге), обвязка соединений выпускает по ним билет к SPN
строки, тело инструмента работает этим билетом внутри песочницы. Каждый тест
спрашивает у самого сервиса, кем он видит клиента.

Стенд: живой KDC, postgres и clickhouse домена; web-инструмент ходит в
HTTP-интерфейс того же clickhouse по Negotiate. Учётка приложения заведена в
обоих сервисах и значится в msDS-AllowedToDelegateTo.
"""

from __future__ import annotations

import base64
import os
import secrets as std_secrets
import shutil
from pathlib import Path
from typing import Any
from uuid import UUID

import krb5
import pytest
from chainlit.user import PersistedUser
from chainlit.user import User as ChainlitUser
from chainlit_stand import SsoStand, StandTokens, enter_context
from gssapi import Credentials, Name, NameType, SecurityContext
from psycopg import sql
from pydantic import SecretStr
from test_tools_integration import Call, ToolSetup

from boba.auth.credentials import KerberosCredentialSource, NoRefresh
from boba.chainlit.auth.kerberos import KerberosAuth
from boba.chainlit.data.data_layer import PostgresDataLayer
from boba.config import bind
from boba.connection_broker.store import ConnectionsConfig, ConnectionStore
from boba.connections.manifest import ConnectionTypes
from boba.connections.marks import ConnectionRefusal
from boba.connections.sealed import ConnectionRefs
from boba.connections.stored import ConnectionBase, GrantTarget
from boba.db.clickhouse.connection import ClickHouseConfig
from boba.db.postgres import AsyncPostgresPool
from boba.db.postgres.connection import PostgresConfig
from boba.identity.errors import RefusalError
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
from boba.sandbox.zygote import ZygoteRegistry
from boba.stand.connections import StandUserConnections
from boba.stand.site import Stand
from boba.tool.ch.tools import ChToolConfig
from boba.tool.pg.tools import PgToolConfig
from boba.tool.web.tools import WebToolsConfig
from boba.toolkit.entry import ToolMain
from boba.toolkit.wrap import ToolProcessWrap
from boba.toolrun.bridge import ToolBridge
from boba.toolrun.injected import InjectedConfig
from boba.transport.http.connection import HttpConnection, NegotiateAuth, UrlScheme

_REPO = Path(__file__).resolve().parents[4]
_SANDBOX_STAGING = _REPO / "build" / "chainlit" / "src" / "sandbox"
_ROOTFS_IMAGE = _SANDBOX_STAGING / "plugins" / "boba-tool-shell" / "rootfs.ext4"
_CGROUP_BASE = os.environ.get("BOBA_CGROUP_BASE", "/sys/fs/cgroup/boba")

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


def _cgroup_delegated() -> bool:
    base_ok = os.access(os.path.join(_CGROUP_BASE, "cgroup.procs"), os.W_OK)
    root_ok = os.access("/sys/fs/cgroup/cgroup.procs", os.W_OK)
    return base_ok and root_ok


pytestmark = [
    pytest.mark.integration,
    pytest.mark.anyio,
    pytest.mark.skipif(
        shutil.which("bwrap") is None or not _ROOTFS_IMAGE.exists(),
        reason="нет bwrap или артефактов песочницы (собрать: make fetch sandbox)",
    ),
    pytest.mark.skipif(
        not _cgroup_delegated(),
        reason=f"cgroup base {_CGROUP_BASE} не делегирован пользователю",
    ),
    pytest.mark.skipif(
        not SERVICE_KEYTAB.is_file() or not KRB5_CONF.is_file(),
        reason="нет keytab/krb5.conf локального AD",
    ),
]


def _key() -> SecretStr:
    return SecretStr(base64.b64encode(std_secrets.token_bytes(32)).decode())


@pytest.fixture(scope="module", autouse=True)
def stop_zygotes():
    try:
        yield
    finally:
        ZygoteRegistry.stop_all()


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
def user_password(raw_config: Any) -> str:
    """Пароль тестового пользователя домена; браузер им получает свой TGT."""
    from omegaconf import OmegaConf

    return str(OmegaConf.select(raw_config, "site.ldap_bind_password"))


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
    layer: PostgresDataLayer, sso_login: tuple[SsoTickets, str]
) -> PersistedUser:
    """Пользователь чата, вошедший этим SSO-входом: метки лежат в JWT сессии."""
    from chainlit.auth.jwt import create_jwt
    from chainlit.context import init_http_context

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

    token = create_jwt(StandTokens.user(user.identifier, metadata))
    context = init_http_context(user=user, auth_token=token, thread_id=THREAD)
    context.session.chat_profile = PROFILE
    enter_context()
    return user


class Tools:
    """Инструменты секции с боевой обвязкой соединений пользователя."""

    @staticmethod
    def of(  # noqa: PLR0913 — секция описывается всеми своими частями сразу
        raw_config: Any,
        store: ConnectionStore,
        tickets: SsoTickets,
        *,
        section: str,
        module_name: str,
        config_model: type,
        kind: str,
    ) -> dict[str, Any]:
        from importlib import import_module, reload

        module = reload(import_module(module_name))
        launcher = ToolSetup.caller(raw_config, section, [module.__name__])

        functions = [ToolBridge.as_structured_tool(tool) for tool in module.TOOLS]
        ToolProcessWrap.guard_all(ToolMain.toolset(*functions), launcher)

        def resolve(name: str, annotation: Any) -> object:
            return bind(raw_config, path=f"tool.{section}", model=config_model)

        StandUserConnections(
            lambda: store,
            lambda: KerberosCredentialSource(
                tickets, BusRefreshSignal(lambda: MemoryMessageBus("test"))
            ),
            ConnectionTypes.discover,
        ).bind_all(functions)
        InjectedConfig.bind_all(functions, resolve)

        return ToolSetup.by_name(functions)


def _credentials() -> KerberosCredentialSource:
    return KerberosCredentialSource(None, NoRefresh())


@pytest.fixture
def pg_tools(raw_config: Any, store: ConnectionStore, tickets: SsoTickets):
    return Tools.of(
        raw_config,
        store,
        tickets,
        section="pg",
        module_name="boba.tool.pg.tools",
        config_model=PgToolConfig,
        kind="postgres",
    )


@pytest.fixture
def ch_tools(raw_config: Any, store: ConnectionStore, tickets: SsoTickets):
    return Tools.of(
        raw_config,
        store,
        tickets,
        section="ch",
        module_name="boba.tool.ch.tools",
        config_model=ChToolConfig,
        kind="clickhouse",
    )


@pytest.fixture
def web_tools(raw_config: Any, store: ConnectionStore, tickets: SsoTickets):
    return Tools.of(
        raw_config,
        store,
        tickets,
        section="web",
        module_name="boba.tool.web.tools",
        config_model=WebToolsConfig,
        kind="web",
    )


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
def delegated_pg(raw_config: Any) -> PostgresConfig:
    service = bind(raw_config, path="postgres", model=PostgresConfig)
    return service.model_copy(update={"auth": _delegated()})


@pytest.fixture
def delegated_ch(raw_config: Any) -> ClickHouseConfig:
    service = bind(raw_config, path="clickhouse", model=ClickHouseConfig)
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
    pg_tools: dict[str, Any],
    store: ConnectionStore,
    session: PersistedUser,
    delegated_pg: PostgresConfig,
) -> None:
    await _granted(store, session, "pg-me", delegated_pg)

    result = await Call.ok(
        pg_tools["pg_query"],
        connection="pg-me",
        sql="select current_user as who",
        offset=0,
        limit=50,
    )

    rows = result.statements[0].rows
    if rows != [{"who": ROLE_NAME}]:
        raise AssertionError(f"postgres must see the principal: {rows}")


async def test_clickhouse_query_runs_as_the_signed_in_principal(
    ch_tools: dict[str, Any],
    store: ConnectionStore,
    session: PersistedUser,
    delegated_ch: ClickHouseConfig,
) -> None:
    await _granted(store, session, "ch-me", delegated_ch)

    result = await Call.ok(
        ch_tools["ch_query"],
        connection="ch-me",
        sql="select currentUser() as who",
        offset=0,
        limit=50,
    )

    rows = result.statements[0].rows
    if rows != [{"who": ROLE_NAME}]:
        raise AssertionError(f"clickhouse must see the principal: {rows}")


async def test_web_page_is_fetched_as_the_signed_in_principal(
    web_tools: dict[str, Any],
    store: ConnectionStore,
    session: PersistedUser,
    delegated_web: HttpConnection,
) -> None:
    await _granted(store, session, "ch-http", delegated_web)

    url = delegated_web.url_of("/").copy_merge_params({"query": "select currentUser()"})
    result = await Call.ok(
        web_tools["web_fetch_page"],
        url=str(url),
        connection="ch-http",
        as_markdown=False,
        line_offset=0,
        line_count=5,
    )

    if ROLE_NAME not in result.text.splitlines():
        raise AssertionError(f"clickhouse must see the principal: {result.text!r}")


async def test_targets_list_only_granted_connections(  # noqa: PLR0913 — три вида сразу
    pg_tools: dict[str, Any],
    catalog: Any,
    ch_tools: dict[str, Any],
    web_tools: dict[str, Any],
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
    pg_tools: dict[str, Any],
    store: ConnectionStore,
    session: PersistedUser,
    delegated_pg: PostgresConfig,
) -> None:
    connection_id = await store.add("pg-me", delegated_pg)
    target = GrantTarget.user(UUID(session.id))
    await store.grant(connection_id, target)

    await Call.ok(
        pg_tools["pg_query"],
        connection="pg-me",
        sql="select 1 as one",
        offset=0,
        limit=50,
    )

    await store.revoke(connection_id, target)

    with pytest.raises(RefusalError) as caught:
        await Call.result(
            pg_tools["pg_query"],
            connection="pg-me",
            sql="select 1 as one",
            offset=0,
            limit=50,
        )

    if caught.value.kind != ConnectionRefusal.NOT_VISIBLE:
        raise AssertionError(f"unexpected refusal kind: {caught.value.kind}")
