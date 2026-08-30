"""Соединения пользователя из таблицы доезжают до pg-инструмента в песочнице.

Стенд: пользователь в users, его соединение в connections, грант в grants;
инструмент собран боевой обвязкой и вызван из сессии этого пользователя.
"""

from __future__ import annotations

import base64
import os
import secrets as std_secrets
import shutil
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from chainlit.user import PersistedUser
from chainlit.user import User as ChainlitUser
from chainlit_stand import SsoStand, enter_context
from psycopg import sql
from pydantic import SecretStr
from test_tools_integration import Call, ToolSetup

from boba.auth.credentials import KerberosCredentialSource
from boba.chainlit.auth.kerberos import KerberosAuth
from boba.chainlit.data.data_layer import PostgresDataLayer
from boba.chainlit.infra.kerberos_refresh import ChatRefreshSignal
from boba.config import bind
from boba.connection_broker.store import ConnectionsConfig, ConnectionStore
from boba.connection_broker.user_connections import UserConnections
from boba.connections.http import HttpProfile, NegotiateAuth
from boba.connections.marks import ConnectionRefusal, UserConnectionsSpec
from boba.connections.postgres import PasswordAuth, PostgresConfig
from boba.connections.profile import ConnectionKind, GrantTarget, StoredRole
from boba.connections.whitelist import ConnectionKeying
from boba.db.postgres import AsyncPostgresPool
from boba.identity.context import CallContext, ContextKind
from boba.identity.errors import RefusalError
from boba.identity.session import UserMetadataField
from boba.kerberos import DelegatedAuth, DelegationMode, KeytabAuth
from boba.krb import KeytabCredentials
from boba.krb.seal import SsoTickets
from boba.messaging import MemoryMessageBus
from boba.runtime.plugins import ToolBridge
from boba.sandbox.wrap import ToolProcessWrap
from boba.sandbox.zygote import ZygoteRegistry
from boba.stand.site import Stand
from boba.tool.pg.tools import PgToolConfig
from boba.tool.web.tools import WebGrepConfig
from boba.toolkit.entry import ToolMain
from boba.toolkit.launcher import PayloadFailureError
from boba.toolkit.sql import SqlErrorKind
from boba.toolrun.injected import InjectedConfig

_REPO = Path(__file__).resolve().parents[4]
_ROOTFS_IMAGE = _REPO / "build" / "chainlit" / "src" / "sandbox" / "rootfs.ext4"
_CGROUP_BASE = os.environ.get("BOBA_CGROUP_BASE", "/sys/fs/cgroup/boba")

SCHEMA = "connections_e2e"
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


@pytest.fixture
def service_pg(raw_config: Any) -> PostgresConfig:
    return bind(raw_config, path="postgres", model=PostgresConfig)


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
def pg_tools(
    raw_config: Any, store: ConnectionStore, sso: tuple[SsoTickets, str]
) -> dict[str, Any]:
    """pg-инструменты с боевой обвязкой соединений пользователя."""
    from importlib import reload

    import boba.tool.pg.tools as pg_module

    module = reload(pg_module)
    launcher = ToolSetup.caller(raw_config, "pg", [module.__name__])

    functions = [ToolBridge.as_structured_tool(tool) for tool in module.TOOLS]
    ToolProcessWrap.guard_all(ToolMain.toolset(*functions), launcher)

    def resolve(name: str, annotation: Any) -> object:
        return bind(raw_config, path="tool.pg", model=PgToolConfig)

    spec = UserConnectionsSpec(ConnectionKind.POSTGRES, ConnectionKeying.NAME)
    UserConnections.bind_all(
        functions,
        lambda: store,
        lambda: KerberosCredentialSource(
            sso[0], ChatRefreshSignal(lambda: MemoryMessageBus("test"))
        ),
        spec,
        resolve,
    )
    InjectedConfig.bind_all(functions, resolve)

    return ToolSetup.by_name(functions)


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
    def enter(user: PersistedUser) -> None:
        """Сессия с JWT входа: роли берутся из токена, а не из строки users."""
        from chainlit.auth.jwt import create_jwt
        from chainlit.context import init_http_context

        context = init_http_context(
            user=user, thread_id=THREAD, auth_token=create_jwt(user)
        )
        context.session.chat_profile = PROFILE
        enter_context()

    @staticmethod
    def enter_sso(user: PersistedUser, principal: str, sealed: str) -> None:
        """Сессия с JWT SSO-входа: провайдер, принципал и метка входа."""
        from chainlit.auth.jwt import create_jwt
        from chainlit.context import init_http_context

        metadata = {
            UserMetadataField.ROLES: [ROLE],
            UserMetadataField.PROVIDER: KerberosAuth.__name__,
            UserMetadataField.PRINCIPAL: principal,
            UserMetadataField.TICKET: sealed,
        }
        token = create_jwt(ChainlitUser(identifier=user.identifier, metadata=metadata))
        context = init_http_context(user=user, auth_token=token, thread_id=THREAD)
        context.session.chat_profile = PROFILE
        enter_context()


async def test_granted_connection_is_visible_and_works(
    pg_tools: dict[str, Any],
    store: ConnectionStore,
    layer: PostgresDataLayer,
    service_pg: PostgresConfig,
) -> None:
    user = await Session.user(layer, "conn-owner")
    connection_id = await store.add("main", service_pg)
    await store.grant(connection_id, GrantTarget.user(UUID(user.id)))
    Session.enter(user)

    targets = await Call.ok(pg_tools["pg_connection_list"])
    names = [row["connection_name"] for row in targets.rows]
    if names != ["main"]:
        raise AssertionError(f"whitelist must hold the granted row only: {names}")

    result = await Call.ok(
        pg_tools["pg_query"], connection_name="main", sql="select 1 as answer"
    )
    if result.rows != [{"answer": 1}]:
        raise AssertionError(f"query must run on the granted connection: {result}")


async def test_role_grant_reaches_every_role_holder(
    pg_tools: dict[str, Any],
    store: ConnectionStore,
    layer: PostgresDataLayer,
    service_pg: PostgresConfig,
) -> None:
    user = await Session.user(layer, "conn-role-holder")
    roles = StoredRole.by_name(await store.roles())
    connection_id = await store.add("shared", service_pg)
    await store.grant(connection_id, GrantTarget.role(roles[ROLE]))
    Session.enter(user)

    targets = await Call.ok(pg_tools["pg_connection_list"])
    names = [row["connection_name"] for row in targets.rows]
    if names != ["shared"]:
        raise AssertionError(f"role grant must be visible: {names}")


async def test_stranger_sees_nothing(
    pg_tools: dict[str, Any],
    store: ConnectionStore,
    layer: PostgresDataLayer,
    service_pg: PostgresConfig,
) -> None:
    owner = await Session.user(layer, "conn-owner-2")
    stranger = await Session.user(layer, "conn-stranger")
    connection_id = await store.add("main", service_pg)
    await store.grant(connection_id, GrantTarget.user(UUID(owner.id)))
    Session.enter(stranger)

    targets = await Call.ok(pg_tools["pg_connection_list"])
    if targets.rows:
        raise AssertionError(f"stranger must see no connections: {targets.rows}")

    with pytest.raises(PayloadFailureError) as caught:
        await Call.result(pg_tools["pg_query"], connection_name="main", sql="select 1")

    if caught.value.kind != SqlErrorKind.UNKNOWN_TARGET:
        raise AssertionError(f"unexpected failure kind: {caught.value.kind}")


async def test_revoke_applies_to_the_next_call(
    pg_tools: dict[str, Any],
    store: ConnectionStore,
    layer: PostgresDataLayer,
    service_pg: PostgresConfig,
) -> None:
    user = await Session.user(layer, "conn-revoked")
    connection_id = await store.add("main", service_pg)
    target = GrantTarget.user(UUID(user.id))
    await store.grant(connection_id, target)
    Session.enter(user)

    before = await Call.ok(pg_tools["pg_connection_list"])
    if not before.rows:
        raise AssertionError("granted row must be visible before revoke")

    await store.revoke(connection_id, target)

    after = await Call.ok(pg_tools["pg_connection_list"])
    if after.rows:
        raise AssertionError("revoked row must disappear without a restart")


async def test_ambiguous_name_is_refused(
    pg_tools: dict[str, Any],
    store: ConnectionStore,
    layer: PostgresDataLayer,
    service_pg: PostgresConfig,
) -> None:
    user = await Session.user(layer, "conn-ambiguous")
    first = await store.add("main", service_pg)
    second = await store.add("main", service_pg)
    await store.grant(first, GrantTarget.user(UUID(user.id)))
    await store.grant(second, GrantTarget.user(UUID(user.id)))
    Session.enter(user)

    targets = await Call.ok(pg_tools["pg_connection_list"])
    if targets.rows:
        raise AssertionError(f"ambiguous name must not be listed: {targets.rows}")

    with pytest.raises(RefusalError) as caught:
        await Call.result(pg_tools["pg_query"], connection_name="main", sql="select 1")

    if caught.value.kind != ConnectionRefusal.AMBIGUOUS:
        raise AssertionError(f"unexpected refusal kind: {caught.value.kind}")


async def test_delegated_connection_runs_as_the_session_principal(
    sso: tuple[SsoTickets, str],
    pg_tools: dict[str, Any],
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
    Session.enter_sso(user, SERVICE_PRINCIPAL, sso[1])

    result = await Call.ok(
        pg_tools["pg_query"],
        connection_name="mine",
        sql="select current_user as who",
    )
    if result.rows != [{"who": SERVICE_USER}]:
        raise AssertionError(f"query must run as the delegated principal: {result}")


async def test_delegated_connection_refuses_local_login(
    pg_tools: dict[str, Any],
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
    Session.enter(user)

    with pytest.raises(RefusalError) as caught:
        await Call.result(pg_tools["pg_query"], connection_name="mine", sql="select 1")

    if caught.value.kind != ConnectionRefusal.NO_DELEGATION:
        raise AssertionError(f"unexpected refusal kind: {caught.value.kind}")


async def test_unreachable_database_is_reported_by_the_body(
    pg_tools: dict[str, Any],
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
    Session.enter(user)

    with pytest.raises(PayloadFailureError) as caught:
        await Call.result(pg_tools["pg_query"], connection_name="dead", sql="select 1")

    if caught.value.kind != SqlErrorKind.DATABASE_UNAVAILABLE:
        raise AssertionError(f"unexpected failure kind: {caught.value.kind}")


@pytest.fixture
def web_tools(
    raw_config: Any, store: ConnectionStore, sso: tuple[SsoTickets, str]
) -> dict[str, Any]:
    """web-инструменты с боевой обвязкой соединений пользователя."""
    from importlib import reload

    import boba.tool.web.tools as web_module

    module = reload(web_module)
    launcher = ToolSetup.caller(raw_config, "web", [module.__name__])

    functions = [ToolBridge.as_structured_tool(tool) for tool in module.TOOLS]
    ToolProcessWrap.guard_all(ToolMain.toolset(*functions), launcher)

    def resolve(name: str, annotation: Any) -> object:
        return bind(raw_config, path="tool.web", model=WebGrepConfig)

    spec = UserConnectionsSpec(ConnectionKind.WEB, ConnectionKeying.NAME)
    UserConnections.bind_all(
        functions,
        lambda: store,
        lambda: KerberosCredentialSource(
            sso[0], ChatRefreshSignal(lambda: MemoryMessageBus("test"))
        ),
        spec,
        resolve,
    )
    InjectedConfig.bind_all(functions, resolve)

    return ToolSetup.by_name(functions)


@pytest.mark.skipif(
    not STAND.ch_addr, reason="в конфиге стенда нет clickhouse (ch_addr)"
)
async def test_web_negotiate_connection_authenticates_as_the_principal(
    sso: tuple[SsoTickets, str],
    web_tools: dict[str, Any],
    store: ConnectionStore,
    layer: PostgresDataLayer,
) -> None:
    """Web-строка negotiate/delegated: HTTP-интерфейс ClickHouse видит принципал."""
    user = await Session.user(layer, "conn-web-negotiate")
    row = HttpProfile(
        base_url=CH_URL,
        ssl_verify=False,
        auth=NegotiateAuth(
            method="negotiate",
            kerberos=DelegatedAuth(method="kerberos_delegated"),
            service_host=STAND.ch_host,
        ),
    )
    connection_id = await store.add("ch-http", row)
    await store.grant(connection_id, GrantTarget.user(UUID(user.id)))
    Session.enter_sso(user, SERVICE_PRINCIPAL, sso[1])

    result = await Call.ok(
        web_tools["web_fetch_page"],
        url=f"{CH_URL}/?query=select%20currentUser()",
        connection_name="ch-http",
        as_markdown=False,
        line_offset=0,
        line_count=5,
    )
    if SERVICE_USER not in result.text:
        raise AssertionError(f"clickhouse must see the principal: {result.text}")


async def test_call_outside_session_is_refused(pg_tools: dict[str, Any]) -> None:
    from chainlit.context import init_http_context

    init_http_context(user=None)
    CallContext.reset()

    with pytest.raises(RefusalError) as caught:
        await Call.result(pg_tools["pg_connection_list"])

    if caught.value.kind != ContextKind.NO_CONTEXT:
        raise AssertionError(f"unexpected refusal kind: {caught.value.kind}")
