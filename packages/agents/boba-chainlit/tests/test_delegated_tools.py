"""Вызовы инструментов на делегированном kerberos: pg, ch и web в песочнице.

Полный боевой путь: SPNEGO-accept собирает evidence-креды входа (режим
constrained, как в конфиге), обвязка соединений выпускает по ним билет к SPN
строки, тело инструмента работает этим билетом внутри песочницы. Каждый тест
спрашивает у самого сервиса, кем он видит клиента.

Стенд: живой KDC, postgres, clickhouse и confluence домена; учётка приложения
заведена во всех трёх сервисах и значится в msDS-AllowedToDelegateTo.
"""

from __future__ import annotations

import base64
import json
import os
import secrets as std_secrets
import shutil
from pathlib import Path
from typing import Any

import krb5
import pytest
from chainlit.user import PersistedUser
from chainlit.user import User as ChainlitUser
from conftest import SsoStand, enter_context
from gssapi import Credentials, Name, NameType, SecurityContext
from psycopg import sql
from pydantic import SecretStr
from stand_site import Stand
from test_tools_integration import Call, ToolSetup

from boba.chainlit.auth.kerberos import KerberosAuth
from boba.chainlit.data.data_layer import PostgresDataLayer
from boba.chainlit.infra.kerberos_refresh import ChatRefreshSignal
from boba.connection_broker.store import ConnectionsConfig, ConnectionStore
from boba.connection_broker.user_connections import UserConnections
from boba.connections.clickhouse import ClickHouseConfig
from boba.connections.http import HttpProfile, NegotiateAuth
from boba.connections.kerberos import (
    AcceptConfig,
    ConstrainedDelegation,
    DelegatedAuth,
)
from boba.connections.marks import UserConnectionsSpec
from boba.connections.postgres import PostgresConfig
from boba.connections.profile import ConnectionKind, ConnectionProfile, GrantTarget
from boba.connections.whitelist import ConnectionKeying
from boba.db.postgres import AsyncPostgresPool
from boba.identity.session import UserMetadataField
from boba.krb import SpnegoAcceptor, TicketCapture
from boba.krb.seal import SsoTickets
from boba.runtime.plugins import ToolBridge
from boba.sandbox.wrap import ToolProcessWrap
from boba.sandbox.zygote import ZygoteRegistry
from boba.settings import bind
from boba.tool.ch.tools import ChToolConfig
from boba.tool.pg.tools import PgToolConfig
from boba.tool.web.tools import WebGrepConfig
from boba.toolkit.entry import ToolMain
from boba.toolrun.injected import InjectedConfig

_REPO = Path(__file__).resolve().parents[4]
_ROOTFS_IMAGE = _REPO / "build" / "src" / "sandbox" / "rootfs.ext4"
_CGROUP_BASE = os.environ.get("BOBA_CGROUP_BASE", "/sys/fs/cgroup/boba")

STAND = Stand.required()
KRB5_CONF = Path(STAND.krb_config)
SERVICE_KEYTAB = Path(STAND.krb_http_keytab)
SERVICE_SPN = f"HTTP/{STAND.krb_domain}@{STAND.krb_realm}"
PRINCIPAL = STAND.reader_principal
ROLE_NAME = PRINCIPAL.split("@")[0]
"""Как принципал выглядит для сервисов: роль postgres, пользователь ch и confluence.

Клиентом входа выступает обычный пользователь домена: у сервисной учётки
accept и initiate совпадают, и evidence-креды KDC для неё не выдаёт.
"""

SCHEMA = "delegated_tools"
ROLE = "analyst"
THREAD = "44444444-4444-4444-4444-444444444444"
PROFILE = "test"
CONFLUENCE_LOGIN = "/plugins/servlet/kerberos/ntlm/login"


def _cgroup_delegated() -> bool:
    base_ok = os.access(os.path.join(_CGROUP_BASE, "cgroup.procs"), os.W_OK)
    root_ok = os.access("/sys/fs/cgroup/cgroup.procs", os.W_OK)
    return base_ok and root_ok


pytestmark = [
    pytest.mark.integration,
    pytest.mark.anyio,
    pytest.mark.skipif(
        shutil.which("bwrap") is None or not _ROOTFS_IMAGE.exists(),
        reason="нет bwrap или артефактов песочницы (собрать: make deps)",
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
    built = ConnectionStore(cfg, pool)
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
def sso_login(tmp_path: Path, user_password: str) -> tuple[SsoTickets, str]:
    """Вход по SPNEGO: открыватель билетов и запечатанный билет этого входа."""
    delegation = ConstrainedDelegation(
        service_ccache=f"FILE:{tmp_path / 'service'}",
        krb5_config=str(KRB5_CONF),
    )
    accept = AcceptConfig(service_name=SERVICE_SPN, keytab=str(SERVICE_KEYTAB))
    token = Browser.token(tmp_path, user_password)
    identity = SpnegoAcceptor(accept, delegation).accept(token)
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

    token = create_jwt(ChainlitUser(identifier=user.identifier, metadata=metadata))
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
        kind: ConnectionKind,
    ) -> dict[str, Any]:
        from importlib import import_module, reload

        module = reload(import_module(module_name))
        launcher = ToolSetup.caller(raw_config, section, [module.__name__])

        functions = [ToolBridge.as_structured_tool(tool) for tool in module.TOOLS]
        ToolProcessWrap.guard_all(ToolMain.toolset(*functions), launcher)

        def resolve(name: str, annotation: Any) -> object:
            return bind(raw_config, path=f"tool.{section}", model=config_model)

        spec = UserConnectionsSpec(kind, ConnectionKeying.NAME)
        UserConnections.bind_all(
            functions,
            lambda: store,
            lambda: tickets,
            spec,
            resolve,
            ChatRefreshSignal(),
        )
        InjectedConfig.bind_all(functions, resolve)

        return ToolSetup.by_name(functions)


@pytest.fixture
def pg_tools(raw_config: Any, store: ConnectionStore, tickets: SsoTickets):
    return Tools.of(
        raw_config,
        store,
        tickets,
        section="pg",
        module_name="boba.tool.pg.tools",
        config_model=PgToolConfig,
        kind=ConnectionKind.POSTGRES,
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
        kind=ConnectionKind.CLICKHOUSE,
    )


@pytest.fixture
def web_tools(raw_config: Any, store: ConnectionStore, tickets: SsoTickets):
    return Tools.of(
        raw_config,
        store,
        tickets,
        section="web",
        module_name="boba.tool.web.tools",
        config_model=WebGrepConfig,
        kind=ConnectionKind.WEB,
    )


def _delegated() -> DelegatedAuth:
    """Строка «идёт сам пользователь»: креды даёт его вход в приложение."""
    return DelegatedAuth(method="kerberos_delegated")


async def _granted(
    store: ConnectionStore,
    session: PersistedUser,
    name: str,
    profile: ConnectionProfile,
) -> None:
    connection_id = await store.add(name, profile)
    await store.grant(connection_id, GrantTarget.user(int(session.id)))


@pytest.fixture
def delegated_pg(raw_config: Any) -> PostgresConfig:
    service = bind(raw_config, path="postgres", model=PostgresConfig)
    return service.model_copy(update={"auth": _delegated()})


@pytest.fixture
def delegated_ch(raw_config: Any) -> ClickHouseConfig:
    service = bind(raw_config, path="clickhouse", model=ClickHouseConfig)
    return service.model_copy(update={"auth": _delegated()})


@pytest.fixture
def delegated_confluence(raw_config: Any) -> HttpProfile:
    from omegaconf import OmegaConf

    base_url = str(OmegaConf.select(raw_config, "site.confluence_url"))
    return HttpProfile(
        base_url=base_url,
        ssl_verify=False,
        timeout_sec=30.0,
        auth=NegotiateAuth(
            method="negotiate",
            kerberos=DelegatedAuth(method="kerberos_delegated"),
            login_path=CONFLUENCE_LOGIN,
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
        pg_tools["pg_query"], connection_name="pg-me", sql="select current_user as who"
    )

    if result.rows != [{"who": ROLE_NAME}]:
        raise AssertionError(f"postgres must see the principal: {result.rows}")


async def test_clickhouse_query_runs_as_the_signed_in_principal(
    ch_tools: dict[str, Any],
    store: ConnectionStore,
    session: PersistedUser,
    delegated_ch: ClickHouseConfig,
) -> None:
    await _granted(store, session, "ch-me", delegated_ch)

    result = await Call.ok(
        ch_tools["ch_query"], connection_name="ch-me", sql="select currentUser() as who"
    )

    if result.rows != [{"who": ROLE_NAME}]:
        raise AssertionError(f"clickhouse must see the principal: {result.rows}")


async def test_confluence_page_is_fetched_as_the_signed_in_principal(
    web_tools: dict[str, Any],
    store: ConnectionStore,
    session: PersistedUser,
    delegated_confluence: HttpProfile,
) -> None:
    await _granted(store, session, "confl", delegated_confluence)

    result = await Call.ok(
        web_tools["web_fetch_page"],
        url=f"{delegated_confluence.base_url}/rest/api/user/current",
        connection_name="confl",
        as_markdown=False,
        line_offset=0,
        line_count=5,
    )

    current = json.loads(result.text)
    if current.get("username") != ROLE_NAME:
        raise AssertionError(f"confluence must see the principal: {current}")


async def test_targets_list_only_granted_connections(  # noqa: PLR0913 — три вида сразу
    pg_tools: dict[str, Any],
    ch_tools: dict[str, Any],
    web_tools: dict[str, Any],
    store: ConnectionStore,
    session: PersistedUser,
    delegated_pg: PostgresConfig,
    delegated_ch: ClickHouseConfig,
    delegated_confluence: HttpProfile,
) -> None:
    """Каждый инструмент видит соединения своего вида и только их."""
    await _granted(store, session, "pg-me", delegated_pg)
    await _granted(store, session, "ch-me", delegated_ch)
    await _granted(store, session, "confl", delegated_confluence)

    pg_targets = await Call.ok(pg_tools["pg_connection_list"])
    ch_targets = await Call.ok(ch_tools["ch_connection_list"])
    web_targets = await Call.ok(web_tools["web_connection_list"])

    if [row["connection_name"] for row in pg_targets.rows] != ["pg-me"]:
        raise AssertionError(f"pg targets: {pg_targets.rows}")
    if [row["connection_name"] for row in ch_targets.rows] != ["ch-me"]:
        raise AssertionError(f"ch targets: {ch_targets.rows}")
    if [row["connection_name"] for row in web_targets.rows] != ["confl"]:
        raise AssertionError(f"web targets: {web_targets.rows}")


async def test_revoked_connection_stops_working_at_once(
    pg_tools: dict[str, Any],
    store: ConnectionStore,
    session: PersistedUser,
    delegated_pg: PostgresConfig,
) -> None:
    connection_id = await store.add("pg-me", delegated_pg)
    target = GrantTarget.user(int(session.id))
    await store.grant(connection_id, target)

    await Call.ok(pg_tools["pg_query"], connection_name="pg-me", sql="select 1 as one")

    await store.revoke(connection_id, target)

    from boba.toolkit.launcher import PayloadFailureError
    from boba.toolkit.sql import SqlErrorKind

    with pytest.raises(PayloadFailureError) as caught:
        await Call.result(
            pg_tools["pg_query"], connection_name="pg-me", sql="select 1 as one"
        )

    if caught.value.kind != SqlErrorKind.UNKNOWN_TARGET:
        raise AssertionError(f"unexpected failure kind: {caught.value.kind}")
