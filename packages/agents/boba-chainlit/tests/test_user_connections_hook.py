"""Что обвязка соединений кладёт в injected-конфиг инструмента на вызов.

Инструмент здесь — перехватчик kwargs: тело не запускается, проверяется сам
конфиг, который уехал бы в песочницу: профиль только запрошенного соединения,
имена остальных, билет вызова вместо kerberos-секции строки.
"""

from __future__ import annotations

import base64
import secrets as std_secrets
from pathlib import Path
from typing import Annotated, Any

import krb5
import pytest
from chainlit.user import PersistedUser
from chainlit.user import User as ChainlitUser
from conftest import enter_context
from langchain_core.tools import StructuredTool
from omegaconf import OmegaConf
from psycopg import sql
from pydantic import BaseModel, SecretStr, create_model
from stand_site import Stand

from boba.chainlit.auth.kerberos import KerberosAuth
from boba.chainlit.data.data_layer import PostgresDataLayer
from boba.chainlit.infra.kerberos_refresh import ChatRefreshSignal
from boba.chainlit.infra.session import ChainlitSession
from boba.connection_broker.store import ConnectionsConfig, ConnectionStore
from boba.connection_broker.user_connections import UserConnections, UserKerberos
from boba.connections.http import HttpProfile, NegotiateAuth
from boba.connections.kerberos import (
    DelegatedAuth,
    DelegationMode,
    KeytabAuth,
    TicketAuth,
)
from boba.connections.marks import ConnectionRefusal, UserConnectionsSpec
from boba.connections.postgres import PostgresConfig, TrustAuth
from boba.connections.profile import ConnectionKind, GrantTarget
from boba.connections.whitelist import ConnectionKeying
from boba.db.postgres import AsyncPostgresPool
from boba.identity.errors import RefusalError
from boba.identity.session import UserMetadataField
from boba.krb import (
    CcacheRegistry,
    KerberosEnv,
    KeytabCredentials,
    TicketCredentials,
    UserCcache,
)
from boba.settings import bind
from boba.tool.pg.tools import PgToolConfig
from boba.tool.web.tools import WebGrepConfig
from boba.toolkit.facade import Injected
from boba.toolrun.injected import InjectedConfig
from boba.transport.http import HttpxAuth

pytestmark = pytest.mark.anyio

STAND = Stand.required()
KRB5_CONF = Path(STAND.krb_config)
SERVICE_KEYTAB = Path(STAND.krb_http_keytab)
SERVICE_PRINCIPAL = STAND.service_principal
SERVICE_USER = STAND.krb_pg_user
READER_PRINCIPAL = STAND.reader_principal
"""Второй пользователь стенда: учётка ldap-bind, её пароль лежит в конфиге."""

CH_URL = f"http://{STAND.ch_addr}:{STAND.ch_port}"
CH_WILDCARD_URL = f"http://*.{STAND.krb_domain}:{STAND.ch_port}"
CH_HOST_URL = f"http://{STAND.ch_host}:{STAND.ch_port}"

needs_ch = pytest.mark.skipif(
    not STAND.ch_addr, reason="в конфиге стенда нет clickhouse (ch_addr)"
)

live_kdc = pytest.mark.skipif(
    not STAND.live(),
    reason="нет keytab/krb5.conf локального AD",
)

SCHEMA = "connections_hook"
ROLE = "analyst"
THREAD = "44444444-4444-4444-4444-444444444444"
PROFILE = "test"


def _key() -> SecretStr:
    return SecretStr(base64.b64encode(std_secrets.token_bytes(32)).decode())


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


@pytest.fixture
def delegated_pg(service_pg: PostgresConfig) -> PostgresConfig:
    """Строка таблицы: в базу идёт сам пользователь, ключей у строки нет."""
    return service_pg.model_copy(
        update={"auth": DelegatedAuth(method="kerberos_delegated")}
    )


class Tgt:
    """TGT в свой FILE-ccache: из keytab сервиса либо по паролю учётки."""

    @staticmethod
    def from_keytab() -> str:
        """Кэш кредов приложения; путь выделяет рабочий каталог."""
        credentials = KeytabCredentials.of(
            KeytabAuth(
                method="kerberos_keytab",
                principal=SERVICE_PRINCIPAL,
                keytab=str(SERVICE_KEYTAB),
            )
        )
        credentials.ensure()
        return credentials.ccache

    @staticmethod
    def from_password(ccache: str, principal: str, password: str) -> None:
        with KerberosEnv.applied({KerberosEnv.CONFIG: str(KRB5_CONF)}):
            context = krb5.init_context()
            name = krb5.parse_name_flags(context, principal.encode())
            options = krb5.get_init_creds_opt_alloc(context)
            krb5.get_init_creds_opt_set_forwardable(options, True)
            creds = krb5.get_init_creds_password(
                context, name, options, password.encode()
            )
            cache = krb5.cc_resolve(context, ccache.encode())
            krb5.cc_initialize(context, cache, name)
            krb5.cc_store_cred(context, cache, creds)


SERVICE_LOGIN = "login-service"
READER_LOGIN = "login-reader"
"""Метки SSO-входов стенда: в бою их выдаёт SpnegoMiddleware случайно."""


@pytest.fixture
def registry(tmp_path: Path, raw_config: Any) -> CcacheRegistry:
    """Реестр делегированных тикетов: TGT сервиса и TGT второй учётки стенда."""
    built = CcacheRegistry(
        mode=DelegationMode.FORWARDED,
        renew=False,
        krb5_config=str(KRB5_CONF),
    )

    service = Tgt.from_keytab()
    built.register(UserCcache(SERVICE_PRINCIPAL, service, SERVICE_LOGIN))

    reader = f"FILE:{tmp_path / 'reader'}"
    password = str(OmegaConf.select(raw_config, "site.ldap_bind_password"))
    Tgt.from_password(reader, READER_PRINCIPAL, password)
    built.register(UserCcache(READER_PRINCIPAL, reader, READER_LOGIN))

    return built


class Capture:
    """Инструмент-перехватчик: возвращает kwargs, с которыми пошло бы тело."""

    @staticmethod
    def tool(raw_config: Any, store: ConnectionStore, registry: CcacheRegistry | None):
        schema = create_model(
            "CaptureArgs",
            connection_name=(str, ...),
            cfg=(Annotated[PgToolConfig, Injected], ...),
        )

        async def body(**kwargs: object) -> dict[str, object]:
            return kwargs

        tool = StructuredTool(
            name="capture",
            description="capture",
            args_schema=schema,
            coroutine=body,
        )

        def resolve(name: str, annotation: Any) -> object:
            return bind(raw_config, path="tool.pg", model=PgToolConfig)

        spec = UserConnectionsSpec(ConnectionKind.POSTGRES, ConnectionKeying.NAME)
        UserConnections.bind_all(
            [tool], lambda: store, lambda: registry, spec, resolve, ChatRefreshSignal()
        )
        InjectedConfig.bind_all([tool], resolve)
        return tool

    @staticmethod
    def web_tool(raw_config: Any, store: ConnectionStore, registry: CcacheRegistry):
        schema = create_model(
            "CaptureWebArgs",
            url=(str, ...),
            connection_name=(str, ...),
            cfg=(Annotated[WebGrepConfig, Injected], ...),
        )

        async def body(**kwargs: object) -> dict[str, object]:
            return kwargs

        tool = StructuredTool(
            name="capture_web",
            description="capture",
            args_schema=schema,
            coroutine=body,
        )

        def resolve(name: str, annotation: Any) -> object:
            return bind(raw_config, path="tool.web", model=WebGrepConfig)

        spec = UserConnectionsSpec(ConnectionKind.WEB, ConnectionKeying.NAME)
        UserConnections.bind_all(
            [tool], lambda: store, lambda: registry, spec, resolve, ChatRefreshSignal()
        )
        InjectedConfig.bind_all([tool], resolve)
        return tool

    @staticmethod
    async def config(tool: StructuredTool, connection_name: str) -> PgToolConfig:
        kwargs = await tool.ainvoke({"connection_name": connection_name})
        cfg = kwargs["cfg"]
        if not isinstance(cfg, PgToolConfig):
            raise AssertionError(f"cfg must be PgToolConfig: {type(cfg)}")
        return cfg


class Session:
    """Сессия chainlit: строка users плюс подписанный JWT входа.

    JWT описывает способ входа; строка users — то, что накопилось по всем
    входам. Тест задаёт их раздельно, как это бывает в бою.
    """

    @staticmethod
    def sso_metadata(principal: str, login: str) -> dict[str, object]:
        return {
            UserMetadataField.ROLES: [ROLE],
            UserMetadataField.PROVIDER: KerberosAuth.__name__,
            UserMetadataField.PRINCIPAL: principal,
            UserMetadataField.LOGIN: login,
        }

    @staticmethod
    def local_metadata() -> dict[str, object]:
        return {
            UserMetadataField.ROLES: [ROLE],
            UserMetadataField.PROVIDER: "LocalAuth",
        }

    @staticmethod
    async def user(
        layer: PostgresDataLayer, identifier: str, metadata: dict[str, object]
    ) -> PersistedUser:
        persisted = await layer.create_user(
            ChainlitUser(identifier=identifier, metadata=metadata)
        )
        if persisted is None:
            raise AssertionError("user was not created")
        return persisted

    @staticmethod
    def enter(user: PersistedUser, login_metadata: dict[str, object]) -> str:
        """Сессия пользователя с JWT данного входа; итог — сам токен."""
        from chainlit.auth.jwt import create_jwt
        from chainlit.context import init_http_context

        token = create_jwt(
            ChainlitUser(identifier=user.identifier, metadata=login_metadata)
        )
        context = init_http_context(user=user, auth_token=token, thread_id=THREAD)
        context.session.chat_profile = PROFILE
        enter_context()
        return token


def _servers(ticket: TicketAuth) -> list[str]:
    credentials = TicketCredentials(ticket)
    with credentials.applied():
        context = krb5.init_context()
        cache = krb5.cc_resolve(context, credentials.ccache.encode())
        return [krb5.unparse_name_flags(context, c.server).decode() for c in cache]


async def test_only_requested_profile_is_shipped(
    raw_config: Any,
    store: ConnectionStore,
    layer: PostgresDataLayer,
    service_pg: PostgresConfig,
) -> None:
    plain = service_pg.model_copy(
        update={"auth": TrustAuth(method="trust", user="boba")}
    )
    user = await Session.user(layer, "hook-plain", Session.local_metadata())
    first = await store.add("alpha", plain)
    second = await store.add("beta", plain)
    await store.grant(first, GrantTarget.user(int(user.id)))
    await store.grant(second, GrantTarget.user(int(user.id)))
    Session.enter(user, dict(user.metadata))

    cfg = await Capture.config(Capture.tool(raw_config, store, None), "alpha")

    if list(cfg.profiles) != ["alpha"]:
        raise AssertionError(f"only the requested profile may ship: {cfg.profiles}")
    if cfg.names != ["alpha", "beta"]:
        raise AssertionError(f"all granted names must ship: {cfg.names}")
    if cfg.targets() != ["alpha", "beta"]:
        raise AssertionError("connection_list must see every granted name")


async def test_client_label_names_the_user_and_the_tool(
    raw_config: Any,
    store: ConnectionStore,
    layer: PostgresDataLayer,
    service_pg: PostgresConfig,
) -> None:
    """Сервер видит, кто и чем подключился: application_name несёт логин и тул."""
    plain = service_pg.model_copy(
        update={"auth": TrustAuth(method="trust", user="boba")}
    )
    user = await Session.user(layer, "hook-label", Session.local_metadata())
    await store.grant(await store.add("alpha", plain), GrantTarget.user(int(user.id)))
    Session.enter(user, dict(user.metadata))

    cfg = await Capture.config(Capture.tool(raw_config, store, None), "alpha")

    shipped = cfg.profiles["alpha"]
    if not isinstance(shipped, PostgresConfig):
        raise AssertionError(f"postgres profile expected: {type(shipped)}")
    if shipped.application_name != "boba:hook-label:capture":
        raise AssertionError(f"unexpected client label: {shipped.application_name}")


async def test_unrequested_call_ships_names_only(
    raw_config: Any,
    store: ConnectionStore,
    layer: PostgresDataLayer,
    service_pg: PostgresConfig,
) -> None:
    plain = service_pg.model_copy(
        update={"auth": TrustAuth(method="trust", user="boba")}
    )
    user = await Session.user(layer, "hook-names", Session.local_metadata())
    connection_id = await store.add("alpha", plain)
    await store.grant(connection_id, GrantTarget.user(int(user.id)))
    Session.enter(user, dict(user.metadata))

    cfg = await Capture.config(Capture.tool(raw_config, store, None), "nothing")

    if cfg.profiles:
        raise AssertionError(f"no profile may ship for an unknown name: {cfg.profiles}")
    if cfg.names != ["alpha"]:
        raise AssertionError(f"names must still ship: {cfg.names}")


@live_kdc
async def test_keytab_row_ships_a_service_ticket_only(
    raw_config: Any,
    store: ConnectionStore,
    layer: PostgresDataLayer,
    service_pg: PostgresConfig,
) -> None:
    user = await Session.user(layer, "hook-keytab", Session.local_metadata())
    connection_id = await store.add("main", service_pg)
    await store.grant(connection_id, GrantTarget.user(int(user.id)))
    Session.enter(user, dict(user.metadata))

    cfg = await Capture.config(Capture.tool(raw_config, store, None), "main")

    shipped = cfg.profiles["main"].auth
    if not isinstance(shipped, TicketAuth):
        raise AssertionError(f"keytab row must ship a ticket: {type(shipped)}")
    if shipped.principal != SERVICE_PRINCIPAL:
        raise AssertionError("ticket must belong to the keytab principal")
    if shipped.service != service_pg.service_name():
        raise AssertionError("ticket must target the connection service")

    servers = _servers(shipped)
    if len(servers) != 1 or not servers[0].startswith("postgres/"):
        raise AssertionError(f"ticket ccache must hold one service ticket: {servers}")


@live_kdc
async def test_delegated_row_uses_the_session_principal(
    raw_config: Any,
    store: ConnectionStore,
    layer: PostgresDataLayer,
    delegated_pg: PostgresConfig,
    registry: CcacheRegistry,
) -> None:
    sso_meta = Session.sso_metadata(SERVICE_PRINCIPAL, SERVICE_LOGIN)
    user = await Session.user(layer, "hook-sso", sso_meta)
    connection_id = await store.add("main", delegated_pg)
    await store.grant(connection_id, GrantTarget.user(int(user.id)))
    Session.enter(user, sso_meta)

    cfg = await Capture.config(Capture.tool(raw_config, store, registry), "main")

    profile = cfg.profiles["main"]
    shipped = profile.auth
    if not isinstance(shipped, TicketAuth):
        raise AssertionError(f"delegated row must ship a ticket: {type(shipped)}")
    if shipped.principal != SERVICE_PRINCIPAL:
        raise AssertionError("ticket must carry the session principal")
    if profile.conn_settings()["user"] != SERVICE_USER:
        raise AssertionError(f"postgres role must follow the principal: {profile}")

    servers = _servers(shipped)
    if len(servers) != 1 or not servers[0].startswith("postgres/"):
        raise AssertionError(f"ticket ccache must hold one service ticket: {servers}")


@live_kdc
async def test_role_shared_delegated_row_gives_each_user_their_own_ticket(
    raw_config: Any,
    store: ConnectionStore,
    layer: PostgresDataLayer,
    delegated_pg: PostgresConfig,
    registry: CcacheRegistry,
) -> None:
    roles = await store.roles()
    connection_id = await store.add("shared", delegated_pg)
    await store.grant(connection_id, GrantTarget.role(roles[ROLE]))
    tool = Capture.tool(raw_config, store, registry)

    first_meta = Session.sso_metadata(SERVICE_PRINCIPAL, SERVICE_LOGIN)
    second_meta = Session.sso_metadata(READER_PRINCIPAL, READER_LOGIN)
    first = await Session.user(layer, "hook-role-a", first_meta)
    second = await Session.user(layer, "hook-role-b", second_meta)

    Session.enter(first, first_meta)
    ticket_a = (await Capture.config(tool, "shared")).profiles["shared"].auth

    Session.enter(second, second_meta)
    ticket_b = (await Capture.config(tool, "shared")).profiles["shared"].auth

    if not isinstance(ticket_a, TicketAuth) or not isinstance(ticket_b, TicketAuth):
        raise AssertionError("both users must receive tickets")
    if ticket_a.principal != SERVICE_PRINCIPAL:
        raise AssertionError("first user must act as their own principal")
    if ticket_b.principal != READER_PRINCIPAL:
        raise AssertionError("second user must act as their own principal")
    if not _servers(ticket_b)[0].startswith("postgres/"):
        raise AssertionError("second user's ccache must hold the service ticket")
    if ticket_a.ccache.get_secret_value() == ticket_b.ccache.get_secret_value():
        raise AssertionError("tickets of different users must differ")


@live_kdc
async def test_delegated_row_refuses_session_without_sso(
    raw_config: Any,
    store: ConnectionStore,
    layer: PostgresDataLayer,
    delegated_pg: PostgresConfig,
    registry: CcacheRegistry,
) -> None:
    user = await Session.user(layer, "hook-local", Session.local_metadata())
    connection_id = await store.add("main", delegated_pg)
    await store.grant(connection_id, GrantTarget.user(int(user.id)))
    Session.enter(user, dict(user.metadata))

    with pytest.raises(RefusalError) as caught:
        await Capture.config(Capture.tool(raw_config, store, registry), "main")

    if caught.value.kind != ConnectionRefusal.NO_DELEGATION:
        raise AssertionError(f"unexpected refusal: {caught.value.kind}")


@live_kdc
async def test_delegated_row_refuses_unknown_principal(
    raw_config: Any,
    store: ConnectionStore,
    layer: PostgresDataLayer,
    delegated_pg: PostgresConfig,
    registry: CcacheRegistry,
) -> None:
    sso_meta = Session.sso_metadata(f"nobody@{STAND.krb_realm}", "login-x")
    user = await Session.user(layer, "hook-no-ticket", sso_meta)
    connection_id = await store.add("main", delegated_pg)
    await store.grant(connection_id, GrantTarget.user(int(user.id)))
    Session.enter(user, sso_meta)

    with pytest.raises(RefusalError) as caught:
        await Capture.config(Capture.tool(raw_config, store, registry), "main")

    if caught.value.kind != ConnectionRefusal.NO_DELEGATION:
        raise AssertionError(f"unexpected refusal: {caught.value.kind}")


async def test_delegated_row_refuses_without_sso_configured(
    raw_config: Any,
    store: ConnectionStore,
    layer: PostgresDataLayer,
    delegated_pg: PostgresConfig,
) -> None:
    sso_meta = Session.sso_metadata(SERVICE_PRINCIPAL, SERVICE_LOGIN)
    user = await Session.user(layer, "hook-no-sso", sso_meta)
    connection_id = await store.add("main", delegated_pg)
    await store.grant(connection_id, GrantTarget.user(int(user.id)))
    Session.enter(user, sso_meta)

    with pytest.raises(RefusalError) as caught:
        await Capture.config(Capture.tool(raw_config, store, None), "main")

    if caught.value.kind != ConnectionRefusal.NO_DELEGATION:
        raise AssertionError(f"unexpected refusal: {caught.value.kind}")


@live_kdc
async def test_stale_users_row_does_not_grant_a_local_login(
    raw_config: Any,
    store: ConnectionStore,
    layer: PostgresDataLayer,
    delegated_pg: PostgresConfig,
    registry: CcacheRegistry,
) -> None:
    """Строка users помнит SSO-вход, но эта сессия вошла по паролю."""
    sso_meta = Session.sso_metadata(SERVICE_PRINCIPAL, SERVICE_LOGIN)
    user = await Session.user(layer, "hook-stale", sso_meta)
    connection_id = await store.add("main", delegated_pg)
    await store.grant(connection_id, GrantTarget.user(int(user.id)))
    Session.enter(user, Session.local_metadata())

    with pytest.raises(RefusalError) as caught:
        await Capture.config(Capture.tool(raw_config, store, registry), "main")

    if caught.value.kind != ConnectionRefusal.NO_DELEGATION:
        raise AssertionError(f"unexpected refusal: {caught.value.kind}")


@live_kdc
async def test_login_label_must_match_its_principal(
    raw_config: Any,
    store: ConnectionStore,
    layer: PostgresDataLayer,
    delegated_pg: PostgresConfig,
    registry: CcacheRegistry,
) -> None:
    """JWT с чужой меткой входа билета не получает."""
    forged = Session.sso_metadata(READER_PRINCIPAL, SERVICE_LOGIN)
    user = await Session.user(layer, "hook-forged", forged)
    connection_id = await store.add("main", delegated_pg)
    await store.grant(connection_id, GrantTarget.user(int(user.id)))
    Session.enter(user, forged)

    with pytest.raises(RefusalError) as caught:
        await Capture.config(Capture.tool(raw_config, store, registry), "main")

    if caught.value.kind != ConnectionRefusal.NO_DELEGATION:
        raise AssertionError(f"unexpected refusal: {caught.value.kind}")


@live_kdc
async def test_logout_forgets_the_delegated_ticket(
    raw_config: Any,
    store: ConnectionStore,
    layer: PostgresDataLayer,
    delegated_pg: PostgresConfig,
    registry: CcacheRegistry,
) -> None:
    sso_meta = Session.sso_metadata(SERVICE_PRINCIPAL, SERVICE_LOGIN)
    user = await Session.user(layer, "hook-logout", sso_meta)
    connection_id = await store.add("main", delegated_pg)
    await store.grant(connection_id, GrantTarget.user(int(user.id)))
    token = Session.enter(user, sso_meta)
    tool = Capture.tool(raw_config, store, registry)

    await Capture.config(tool, "main")

    ticket = ChainlitSession.ticket_of_token(token)
    assert ticket is not None
    UserKerberos(lambda: registry, ChatRefreshSignal()).forget(ticket)

    with pytest.raises(RefusalError) as caught:
        await Capture.config(tool, "main")

    if caught.value.kind != ConnectionRefusal.NO_DELEGATION:
        raise AssertionError(f"unexpected refusal: {caught.value.kind}")
    if registry.of_login(SERVICE_LOGIN) is not None:
        raise AssertionError("logout must drop the ticket of that login")


@live_kdc
@needs_ch
async def test_web_negotiate_row_ships_a_ticket(
    raw_config: Any,
    store: ConnectionStore,
    layer: PostgresDataLayer,
    registry: CcacheRegistry,
) -> None:
    """Web-строка с negotiate/delegated уезжает с билетом к HTTP@host."""
    sso_meta = Session.sso_metadata(SERVICE_PRINCIPAL, SERVICE_LOGIN)
    user = await Session.user(layer, "hook-web-sso", sso_meta)
    row = HttpProfile(
        base_url=CH_URL,
        auth=NegotiateAuth(
            method="negotiate",
            kerberos=DelegatedAuth(method="kerberos_delegated"),
            service_host=STAND.ch_host,
        ),
    )
    connection_id = await store.add("ch-http", row)
    await store.grant(connection_id, GrantTarget.user(int(user.id)))
    Session.enter(user, sso_meta)

    tool = Capture.web_tool(raw_config, store, registry)
    kwargs = await tool.ainvoke(
        {"url": f"{CH_URL}/?query=1", "connection_name": "ch-http"}
    )
    cfg = kwargs["cfg"]
    if not isinstance(cfg, WebGrepConfig):
        raise AssertionError(f"cfg must be WebGrepConfig: {type(cfg)}")

    shipped = cfg.profiles["ch-http"]
    if not isinstance(shipped.auth, NegotiateAuth):
        raise AssertionError("negotiate auth must survive arming")
    ticket = shipped.auth.kerberos
    if not isinstance(ticket, TicketAuth):
        raise AssertionError(f"web row must ship a ticket: {type(ticket)}")
    if ticket.service != STAND.ch_spn:
        raise AssertionError(f"unexpected ticket service: {ticket.service}")
    if ticket.principal != SERVICE_PRINCIPAL:
        raise AssertionError(f"unexpected ticket principal: {ticket.principal}")


@live_kdc
@needs_ch
async def test_wildcard_web_row_binds_the_requested_host(
    raw_config: Any,
    store: ConnectionStore,
    layer: PostgresDataLayer,
    registry: CcacheRegistry,
) -> None:
    """Строка `*.domain` покрывает хост ch: SPN и base_url — от реального хоста."""
    sso_meta = Session.sso_metadata(SERVICE_PRINCIPAL, SERVICE_LOGIN)
    user = await Session.user(layer, "hook-web-wildcard", sso_meta)
    row = HttpProfile(
        base_url=CH_WILDCARD_URL,
        auth=NegotiateAuth(
            method="negotiate", kerberos=DelegatedAuth(method="kerberos_delegated")
        ),
    )
    connection_id = await store.add("any-host", row)
    await store.grant(connection_id, GrantTarget.user(int(user.id)))
    Session.enter(user, sso_meta)

    tool = Capture.web_tool(raw_config, store, registry)
    request = {
        "url": f"{CH_HOST_URL}/?query=1",
        "connection_name": "any-host",
    }
    kwargs = await tool.ainvoke(request)
    cfg = kwargs["cfg"]
    if not isinstance(cfg, WebGrepConfig):
        raise AssertionError(f"cfg must be WebGrepConfig: {type(cfg)}")
    if cfg.hosts != {"any-host": f"*.{STAND.krb_domain}"}:
        raise AssertionError(f"connection_list must show the pattern: {cfg.hosts}")

    shipped = cfg.profiles["any-host"]
    if shipped.base_url != CH_HOST_URL:
        raise AssertionError(f"host must be bound into base_url: {shipped.base_url}")
    if not isinstance(shipped.auth, NegotiateAuth):
        raise AssertionError("negotiate auth must survive arming")
    ticket = shipped.auth.kerberos
    if not isinstance(ticket, TicketAuth):
        raise AssertionError(f"wildcard row must ship a ticket: {type(ticket)}")
    if ticket.service != STAND.ch_spn:
        raise AssertionError(f"SPN must be the concrete host: {ticket.service}")


@live_kdc
async def test_confluence_negotiate_row_logs_in_as_the_principal(
    raw_config: Any,
    store: ConnectionStore,
    layer: PostgresDataLayer,
    registry: CcacheRegistry,
) -> None:
    """Confluence после арминга: Negotiate на login-сервлете, REST видит юзера."""
    import httpx
    from omegaconf import OmegaConf

    base_url = str(OmegaConf.select(raw_config, "site.confluence_url"))
    sso_meta = Session.sso_metadata(READER_PRINCIPAL, READER_LOGIN)
    user = await Session.user(layer, "hook-confl", sso_meta)
    row = HttpProfile(
        base_url=base_url,
        ssl_verify=False,
        auth=NegotiateAuth(
            method="negotiate",
            kerberos=DelegatedAuth(method="kerberos_delegated"),
            login_path="/plugins/servlet/kerberos/ntlm/login",
        ),
    )
    connection_id = await store.add("confl", row)
    await store.grant(connection_id, GrantTarget.user(int(user.id)))
    Session.enter(user, sso_meta)

    tool = Capture.web_tool(raw_config, store, registry)
    request = {
        "url": f"{base_url}/rest/api/user/current",
        "connection_name": "confl",
    }
    kwargs = await tool.ainvoke(request)
    cfg = kwargs["cfg"]
    if not isinstance(cfg, WebGrepConfig):
        raise AssertionError(f"cfg must be WebGrepConfig: {type(cfg)}")
    armed = cfg.profiles["confl"]

    # стенд с self-signed сертификатом
    async with httpx.AsyncClient(verify=False, auth=HttpxAuth.of(armed)) as client:  # noqa: S501
        response = await client.get(f"{base_url}/rest/api/user/current")

    if response.status_code != httpx.codes.OK:
        raise AssertionError(f"confluence refused: {response.status_code}")
    if response.json().get("username") != "readonly":
        msg = f"confluence must see the principal: {response.text[:200]}"
        raise AssertionError(msg)


class TestModelsStayTyped:
    async def test_delegated_section_validates_from_the_table(self) -> None:
        raw = {
            "host": "h",
            "user": "u",
            "dbname": "d",
            "connect_timeout": 5,
            "auth": {"method": "kerberos_delegated"},
        }
        profile = PostgresConfig.model_validate(raw)
        if not isinstance(profile.auth, DelegatedAuth):
            raise AssertionError("method=kerberos_delegated must build DelegatedAuth")

    async def test_base_model_is_untouched(self) -> None:
        if not issubclass(PgToolConfig, BaseModel):
            raise AssertionError("PgToolConfig must stay a pydantic model")
