"""Отказы соединений пользователя: что видит LLM и чат в каждом случае.

Инструмент обёрнут как в приложении — `ToolErrorGuard` превращает исключение
обвязки в `ErrorResult`; тест проверяет kind и текст, которые уйдут в
историю и на экран. Стенд: реальный postgres, живой KDC стенда.
"""

from __future__ import annotations

import base64
import secrets as std_secrets
from pathlib import Path
from typing import Annotated, Any
from uuid import UUID

import krb5
import pytest
from chainlit.user import PersistedUser
from chainlit.user import User as ChainlitUser
from chainlit_stand import SsoStand, StubRefs, enter_context
from langchain_core.tools import StructuredTool
from omegaconf import DictConfig, OmegaConf
from psycopg import sql
from psycopg.types.json import Jsonb
from pydantic import SecretStr, create_model

from boba.chainlit.auth.kerberos import KerberosAuth
from boba.chainlit.data.data_layer import PostgresDataLayer
from boba.chainlit.infra.kerberos_refresh import ChatRefreshSignal
from boba.chainlit.infra.plugins import ChatPlugins
from boba.connection_broker.store import ConnectionsConfig, ConnectionStore
from boba.connection_broker.user_connections import UserConnections, UserKerberos
from boba.connections.http import HttpProfile
from boba.connections.kerberos import DelegatedAuth, DelegationMode, KeytabAuth
from boba.connections.marks import ConnectionRefusal, UserConnectionsSpec
from boba.connections.postgres import PasswordAuth, PostgresConfig
from boba.connections.profile import ConnectionKind, GrantTarget
from boba.connections.whitelist import ConnectionKeying
from boba.db.postgres import AsyncPostgresPool
from boba.identity.session import UserMetadataField
from boba.krb import KeytabCredentials
from boba.krb.seal import SsoTickets, TicketSealer
from boba.messaging import MemoryMessageBus
from boba.sandbox.zygote import ZygoteRegistry
from boba.settings import bind
from boba.stand.site import Stand
from boba.tool.pg.tools import PgToolConfig
from boba.tool.web.tools import WebGrepConfig
from boba.toolkit.facade import Injected
from boba.toolkit.result import ErrorResult, ToolArtifact
from boba.toolrun.errors import ToolErrorGuard
from boba.toolrun.injected import InjectedConfig

pytestmark = pytest.mark.anyio

STAND = Stand.required()
KRB5_CONF = Path(STAND.krb_config)
SERVICE_KEYTAB = Path(STAND.krb_http_keytab)
SERVICE_PRINCIPAL = STAND.service_principal
UNKNOWN_HOST = f"nowhere.{STAND.krb_domain}"

live_kdc = pytest.mark.skipif(
    not STAND.live(),
    reason="нет keytab/krb5.conf локального AD",
)

SCHEMA = "connections_failures"
ROLE = "analyst"
THREAD = "44444444-4444-4444-4444-444444444444"
PROFILE = "test"


def _key() -> SecretStr:
    return SecretStr(base64.b64encode(std_secrets.token_bytes(32)).decode())


@pytest.fixture
def key() -> SecretStr:
    return _key()


@pytest.fixture
async def store(pool: AsyncPostgresPool, key: SecretStr) -> ConnectionStore:
    async with pool.connection() as conn:
        await conn.execute(
            sql.SQL("drop schema if exists {} cascade").format(sql.Identifier(SCHEMA))
        )

    cfg = ConnectionsConfig(enable=True, db_schema=SCHEMA, encryption_key=key)
    built = ConnectionStore(cfg, pool)
    await built.setup()
    await built.sync_roles([ROLE])
    return built


@pytest.fixture
def service_pg(raw_config: Any) -> PostgresConfig:
    return bind(raw_config, path="postgres", model=PostgresConfig)


@pytest.fixture
def delegated_pg(service_pg: PostgresConfig) -> PostgresConfig:
    return service_pg.model_copy(
        update={"auth": DelegatedAuth(method="kerberos_delegated")}
    )


class Tickets:
    """Билеты входа под разные беды: открыватель и запечатанный билет."""

    @staticmethod
    def healthy(tmp_path: Path) -> tuple[SsoTickets, str]:
        credentials = KeytabCredentials.of(
            KeytabAuth(
                method="kerberos_keytab",
                principal=SERVICE_PRINCIPAL,
                keytab=str(SERVICE_KEYTAB),
            )
        )
        credentials.ensure()
        tickets = SsoStand.tickets(str(KRB5_CONF))
        sealed = SsoStand.sealed(
            tickets,
            SERVICE_PRINCIPAL,
            credentials.ccache,
            DelegationMode.FORWARDED,
            3600,
        )
        return tickets, sealed

    @staticmethod
    def expired(tmp_path: Path) -> tuple[SsoTickets, str]:
        """Билет входа с истёкшим сроком: так выглядит просроченный вход."""
        credentials = KeytabCredentials.of(
            KeytabAuth(
                method="kerberos_keytab",
                principal=SERVICE_PRINCIPAL,
                keytab=str(SERVICE_KEYTAB),
            )
        )
        credentials.ensure()
        tickets = SsoStand.tickets(str(KRB5_CONF))
        sealed = SsoStand.sealed(
            tickets,
            SERVICE_PRINCIPAL,
            credentials.ccache,
            DelegationMode.FORWARDED,
            -60,
        )
        return tickets, sealed

    @staticmethod
    def dead_kdc(tmp_path: Path) -> tuple[SsoTickets, str]:
        """KDC недоступен: в билете один TGT (за билетом к базе идти в никуда)."""
        conf = tmp_path / "krb5-dead.conf"
        conf.write_text(Tickets._dead_kdc_conf())
        credentials = KeytabCredentials.of(
            KeytabAuth(
                method="kerberos_keytab",
                principal=SERVICE_PRINCIPAL,
                keytab=str(SERVICE_KEYTAB),
            )
        )
        credentials.ensure()
        tgt_only = f"FILE:{tmp_path / 'tgt-only'}"
        Tickets._copy_tgt(credentials.ccache, tgt_only)
        tickets = SsoTickets(
            sealer=SsoStand.tickets(str(conf)).sealer, krb5_config=str(conf)
        )
        sealed = SsoStand.sealed(
            tickets, SERVICE_PRINCIPAL, tgt_only, DelegationMode.FORWARDED, 3600
        )
        return tickets, sealed

    @staticmethod
    def _copy_tgt(source: str, target: str) -> None:
        """Только TGT источника: кэшированные сервисные билеты остаются позади."""
        context = krb5.init_context()
        origin = krb5.cc_resolve(context, source.encode())
        principal = krb5.cc_get_principal(context, origin)
        cache = krb5.cc_resolve(context, target.encode())
        krb5.cc_initialize(context, cache, principal)
        for cred in origin:
            server = krb5.unparse_name_flags(context, cred.server).decode()
            if not server.startswith("krbtgt/"):
                continue

            krb5.cc_store_cred(context, cache, cred)

    @staticmethod
    def _dead_kdc_conf() -> str:
        realm = SERVICE_PRINCIPAL.split("@", 1)[1]
        return (
            "[libdefaults]\n"
            f"  default_realm = {realm}\n"
            "  dns_lookup_kdc = false\n"
            "  dns_lookup_realm = false\n"
            "  rdns = false\n"
            "  dns_canonicalize_hostname = false\n"
            "  kdc_timeout = 1\n"
            "  max_retries = 1\n"
            "[realms]\n"
            f"  {realm} = {{\n"
            "    kdc = 127.0.0.1:1\n"
            "  }\n"
            "[domain_realm]\n"
            f"  .{realm.lower()} = {realm}\n"
            f"  {realm.lower()} = {realm}\n"
        )


class Session:
    @staticmethod
    def sso(principal: str, sealed: str) -> dict[str, object]:
        return {
            UserMetadataField.ROLES: [ROLE],
            UserMetadataField.PROVIDER: KerberosAuth.__name__,
            UserMetadataField.PRINCIPAL: principal,
            UserMetadataField.TICKET: sealed,
        }

    @staticmethod
    def sso_without_delegation(principal: str) -> dict[str, object]:
        """SSO прошёл, но AD тикет не делегировал: метки входа нет."""
        return {
            UserMetadataField.ROLES: [ROLE],
            UserMetadataField.PROVIDER: KerberosAuth.__name__,
            UserMetadataField.PRINCIPAL: principal,
        }

    @staticmethod
    def local() -> dict[str, object]:
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
    def enter(user: PersistedUser, login_metadata: dict[str, object]) -> None:
        from chainlit.auth.jwt import create_jwt
        from chainlit.context import init_http_context

        token = create_jwt(
            ChainlitUser(identifier=user.identifier, metadata=login_metadata)
        )
        context = init_http_context(user=user, auth_token=token, thread_id=THREAD)
        context.session.chat_profile = PROFILE
        enter_context()


class Guarded:
    """Инструмент с обвязкой соединений и охранником ошибок, как в приложении."""

    @staticmethod
    def pg(raw_config: Any, store: ConnectionStore, tickets: SsoTickets | None):
        schema = create_model(
            "GuardedPgArgs",
            connection_name=(str, ...),
            cfg=(Annotated[PgToolConfig, Injected], ...),
        )

        def resolve(name: str, annotation: Any) -> object:
            return bind(raw_config, path="tool.pg", model=PgToolConfig)

        spec = UserConnectionsSpec(ConnectionKind.POSTGRES, ConnectionKeying.NAME)
        return Guarded._build(schema, store, tickets, spec, resolve)

    @staticmethod
    def web(raw_config: Any, store: ConnectionStore):
        schema = create_model(
            "GuardedWebArgs",
            url=(str, ...),
            connection_name=(str, ...),
            cfg=(Annotated[WebGrepConfig, Injected], ...),
        )

        def resolve(name: str, annotation: Any) -> object:
            return bind(raw_config, path="tool.web", model=WebGrepConfig)

        spec = UserConnectionsSpec(ConnectionKind.WEB, ConnectionKeying.NAME)
        return Guarded._build(schema, store, None, spec, resolve)

    @staticmethod
    def _build(schema, store, tickets, spec, resolve) -> StructuredTool:
        async def body(**kwargs: object) -> tuple[str, dict[str, object]]:
            return "ok", kwargs

        tool = StructuredTool(
            name="guarded",
            description="guarded",
            args_schema=schema,
            coroutine=body,
            response_format="content_and_artifact",
        )

        UserConnections.bind_all(
            [tool],
            lambda: store,
            lambda: tickets,
            spec,
            resolve,
            ChatRefreshSignal(lambda: MemoryMessageBus("test")),
        )
        InjectedConfig.bind_all([tool], resolve)
        ToolErrorGuard.guard_all([tool])
        return tool

    @staticmethod
    async def call(tool: StructuredTool, **args: Any) -> Any:
        message = await tool.ainvoke(
            {"name": tool.name, "args": args, "id": "c1", "type": "tool_call"}
        )
        return message.artifact

    @staticmethod
    async def failure(tool: StructuredTool, **args: Any) -> ErrorResult:
        artifact = await Guarded.call(tool, **args)
        result = ToolArtifact.revive(artifact)
        if not isinstance(result, ErrorResult):
            raise AssertionError(f"expected an ErrorResult, got {result!r}")
        return result


def _expect(result: ErrorResult, kind: str, *phrases: str) -> None:
    if result.error_kind != kind:
        msg = f"kind {result.error_kind!r} != {kind!r}: {result.message}"
        raise AssertionError(msg)

    for phrase in phrases:
        if phrase not in result.message:
            raise AssertionError(f"{phrase!r} not in {result.message!r}")


async def _grant_delegated(
    store: ConnectionStore, delegated_pg: PostgresConfig, user: PersistedUser
) -> UUID:
    connection_id = await store.add("main", delegated_pg)
    await store.grant(connection_id, GrantTarget.user(UUID(user.id)))
    return connection_id


class TestDelegationUnavailable:
    """1.1: делегированный тикет не получить."""

    async def test_sso_not_configured(
        self, raw_config, store, layer, delegated_pg, tmp_path: Path
    ) -> None:
        sso = Session.sso(SERVICE_PRINCIPAL, "sealed-unused")
        user = await Session.user(layer, "f-no-sso", sso)
        await _grant_delegated(store, delegated_pg, user)
        Session.enter(user, sso)

        result = await Guarded.failure(
            Guarded.pg(raw_config, store, None), connection_name="main"
        )

        _expect(
            result,
            ConnectionRefusal.NO_DELEGATION,
            "Kerberos SSO is not configured",
        )

    @live_kdc
    async def test_local_login(
        self, raw_config, store, layer, delegated_pg, tmp_path: Path
    ) -> None:
        user = await Session.user(layer, "f-local", Session.local())
        await _grant_delegated(store, delegated_pg, user)
        Session.enter(user, Session.local())

        result = await Guarded.failure(
            Guarded.pg(raw_config, store, Tickets.healthy(tmp_path)[0]),
            connection_name="main",
        )

        _expect(
            result,
            ConnectionRefusal.NO_DELEGATION,
            "you signed in with LocalAuth",
            "Kerberos SSO button",
        )

    @live_kdc
    async def test_sso_login_without_delegation(
        self, raw_config, store, layer, delegated_pg, tmp_path: Path
    ) -> None:
        """AD не выдал форвардный тикет: вход прошёл, метки входа нет."""
        metadata = Session.sso_without_delegation(SERVICE_PRINCIPAL)
        user = await Session.user(layer, "f-no-delegation", metadata)
        await _grant_delegated(store, delegated_pg, user)
        Session.enter(user, metadata)

        result = await Guarded.failure(
            Guarded.pg(raw_config, store, Tickets.healthy(tmp_path)[0]),
            connection_name="main",
        )

        _expect(
            result,
            ConnectionRefusal.NO_DELEGATION,
            "carried no delegated ticket",
            "Active Directory",
        )

    @live_kdc
    async def test_ticket_sealed_by_another_secret(
        self, raw_config, store, layer, delegated_pg, tmp_path: Path
    ) -> None:
        """Билет запечатан другим секретом (другой инстанс): открыть его нельзя."""
        healthy = Tickets.healthy(tmp_path)
        foreign = SsoTickets(
            sealer=TicketSealer("another-secret"), krb5_config=str(KRB5_CONF)
        )
        sealed = foreign.sealer.seal(healthy[0].open(healthy[1]))
        metadata = Session.sso(SERVICE_PRINCIPAL, sealed)
        user = await Session.user(layer, "f-foreign-secret", metadata)
        await _grant_delegated(store, delegated_pg, user)
        Session.enter(user, metadata)

        result = await Guarded.failure(
            Guarded.pg(raw_config, store, healthy[0]), connection_name="main"
        )

        _expect(
            result,
            ConnectionRefusal.NO_DELEGATION,
            "does not open",
            "sign in again",
        )

    async def test_delegated_ticket_expired(
        self, raw_config, store, layer, delegated_pg, tmp_path: Path
    ) -> None:
        tickets, sealed = Tickets.expired(tmp_path)
        sso = Session.sso(SERVICE_PRINCIPAL, sealed)
        user = await Session.user(layer, "f-expired", sso)
        await _grant_delegated(store, delegated_pg, user)
        Session.enter(user, sso)

        result = await Guarded.failure(
            Guarded.pg(raw_config, store, tickets),
            connection_name="main",
        )

        _expect(result, "CredentialsExpiredError", "expired", "sign in again")

    async def test_kdc_unreachable(
        self, raw_config, store, layer, delegated_pg, tmp_path: Path
    ) -> None:
        tickets, sealed = Tickets.dead_kdc(tmp_path)
        sso = Session.sso(SERVICE_PRINCIPAL, sealed)
        user = await Session.user(layer, "f-kdc", sso)
        await _grant_delegated(store, delegated_pg, user)
        Session.enter(user, sso)

        result = await Guarded.failure(
            Guarded.pg(raw_config, store, tickets),
            connection_name="main",
        )

        _expect(result, "KerberosError", "KDC")

    @live_kdc
    async def test_service_unknown_to_kdc(
        self, raw_config, store, layer, delegated_pg, tmp_path: Path
    ) -> None:
        nowhere = delegated_pg.model_copy(update={"host": UNKNOWN_HOST})
        tickets, sealed = Tickets.healthy(tmp_path)
        sso = Session.sso(SERVICE_PRINCIPAL, sealed)
        user = await Session.user(layer, "f-spn", sso)
        await _grant_delegated(store, nowhere, user)
        Session.enter(user, sso)

        result = await Guarded.failure(
            Guarded.pg(raw_config, store, tickets),
            connection_name="main",
        )

        _expect(result, "KerberosError", UNKNOWN_HOST)

    @live_kdc
    async def test_ticket_too_short_for_the_row(
        self, raw_config, store, layer, service_pg, tmp_path: Path
    ) -> None:
        strict = service_pg.model_copy(
            update={
                "auth": DelegatedAuth(method="kerberos_delegated", min_lifetime=10**9)
            }
        )
        tickets, sealed = Tickets.healthy(tmp_path)
        sso = Session.sso(SERVICE_PRINCIPAL, sealed)
        user = await Session.user(layer, "f-short", sso)
        await _grant_delegated(store, strict, user)
        Session.enter(user, sso)

        result = await Guarded.failure(
            Guarded.pg(raw_config, store, tickets),
            connection_name="main",
        )

        _expect(result, "CredentialsExpiredError", "less than", "sign in again")


class TestRefusalText:
    """Отказ — одна фраза для человека и LLM: свой kind, без цепочки причин."""

    @staticmethod
    def _refusal(result: ErrorResult) -> None:
        for noise in ("<-", "ValidationError", "Traceback", "pydantic"):
            if noise in result.message:
                raise AssertionError(f"{noise!r} leaked into {result.message!r}")

    async def test_kind_is_the_refusal_kind(
        self, raw_config, store, layer, delegated_pg, tmp_path: Path
    ) -> None:
        user = await Session.user(layer, "f-kind", Session.local())
        await _grant_delegated(store, delegated_pg, user)
        Session.enter(user, Session.local())

        result = await Guarded.failure(
            Guarded.pg(raw_config, store, Tickets.healthy(tmp_path)[0]),
            connection_name="main",
        )

        if result.error_kind != ConnectionRefusal.NO_DELEGATION:
            msg = f"kind must classify the refusal: {result.error_kind}"
            raise AssertionError(msg)
        self._refusal(result)

    async def test_message_names_the_provider_and_the_way_out(
        self, raw_config, store, layer, delegated_pg, tmp_path: Path
    ) -> None:
        user = await Session.user(layer, "f-text", Session.local())
        await _grant_delegated(store, delegated_pg, user)
        Session.enter(user, Session.local())

        result = await Guarded.failure(
            Guarded.pg(raw_config, store, Tickets.healthy(tmp_path)[0]),
            connection_name="main",
        )

        for phrase in ("LocalAuth", "Kerberos SSO button", UserKerberos.RETRY_HINT):
            if phrase not in result.message:
                raise AssertionError(f"{phrase!r} not in {result.message!r}")


class TestNoConnections:
    """1.2: соединений нет или они непригодны."""

    async def test_no_grants_at_all(self, raw_config, store, layer) -> None:
        user = await Session.user(layer, "f-empty", Session.local())
        Session.enter(user, Session.local())

        artifact = await Guarded.call(
            Guarded.pg(raw_config, store, None), connection_name="main"
        )

        cfg = artifact["cfg"]
        if cfg.profiles or cfg.names:
            raise AssertionError(f"nothing is granted, whitelist must be empty: {cfg}")

    async def test_store_unavailable(
        self, raw_config, layer, app_config, test_database, key: SecretStr
    ) -> None:
        user = await Session.user(layer, "f-store-down", Session.local())
        Session.enter(user, Session.local())

        closed = AsyncPostgresPool(
            app_config.data_layer.postgres.model_copy(update={"dbname": test_database})
        )
        await closed.open()
        await closed.close()
        cfg = ConnectionsConfig(enable=True, db_schema=SCHEMA, encryption_key=key)
        broken = ConnectionStore(cfg, closed)

        result = await Guarded.failure(
            Guarded.pg(raw_config, broken, None), connection_name="main"
        )

        _expect(result, "ConnectionStoreError", "for subject failed")

    async def test_row_is_not_a_profile(
        self, raw_config, store, layer, pool: AsyncPostgresPool
    ) -> None:
        user = await Session.user(layer, "f-garbage", Session.local())
        async with pool.cursor() as cur:
            await cur.execute(
                sql.SQL(
                    "insert into {} (name, data) values (%s, %s) returning id"
                ).format(sql.Identifier(SCHEMA, "connections")),
                ("broken", Jsonb({"kind": "postgres"})),
            )
            row = await cur.fetchone()
        if row is None:
            raise AssertionError("row must be inserted")
        await store.grant(UUID(str(row[0])), GrantTarget.user(UUID(user.id)))
        Session.enter(user, Session.local())

        result = await Guarded.failure(
            Guarded.pg(raw_config, store, None), connection_name="broken"
        )

        _expect(result, "ConnectionStoreError", "not a valid connection profile")

    async def test_wrong_encryption_key(
        self, raw_config, store, layer, pool: AsyncPostgresPool, service_pg
    ) -> None:
        user = await Session.user(layer, "f-key", Session.local())
        secret = service_pg.model_copy(
            update={
                "auth": PasswordAuth(
                    method="password", user="boba", password=SecretStr("s3cret")
                )
            }
        )
        connection_id = await store.add("main", secret)
        await store.grant(connection_id, GrantTarget.user(UUID(user.id)))
        Session.enter(user, Session.local())

        cfg = ConnectionsConfig(enable=True, db_schema=SCHEMA, encryption_key=_key())
        foreign = ConnectionStore(cfg, pool)

        result = await Guarded.failure(
            Guarded.pg(raw_config, foreign, None), connection_name="main"
        )

        _expect(result, "SecretCryptoError", "not decrypted")

    @pytest.mark.parametrize(
        "auth",
        [
            {
                "method": "kerberos_keytab",
                "principal": SERVICE_PRINCIPAL,
                "keytab": str(SERVICE_KEYTAB),
                "ccache": "MEMORY:stolen",
            },
            {"method": "kerberos_ticket", "principal": SERVICE_PRINCIPAL},
            {"method": "magic", "user": "u"},
        ],
        ids=["keytab-with-own-ccache", "ticket-in-the-table", "unknown-method"],
    )
    async def test_row_with_a_bad_auth_is_not_a_profile(
        self, raw_config, store, layer, pool: AsyncPostgresPool, auth
    ) -> None:
        """Путь кэша, внутренний билет и неизвестный метод профилем не считаются."""
        user = await Session.user(layer, "f-bad-auth", Session.local())
        data = {
            "kind": "postgres",
            "host": "h",
            "dbname": "d",
            "connect_timeout": 5,
            "auth": auth,
        }
        async with pool.cursor() as cur:
            await cur.execute(
                sql.SQL(
                    "insert into {} (name, data) values (%s, %s) returning id"
                ).format(sql.Identifier(SCHEMA, "connections")),
                ("main", Jsonb(data)),
            )
            row = await cur.fetchone()
        if row is None:
            raise AssertionError("row must be inserted")
        await store.grant(UUID(str(row[0])), GrantTarget.user(UUID(user.id)))
        Session.enter(user, Session.local())

        result = await Guarded.failure(
            Guarded.pg(raw_config, store, None), connection_name="main"
        )

        _expect(result, "ConnectionStoreError", "not a valid connection profile")

    async def test_keytab_file_missing(
        self, raw_config, store, layer, service_pg, tmp_path: Path
    ) -> None:
        missing = KeytabAuth(
            method="kerberos_keytab",
            principal=SERVICE_PRINCIPAL,
            keytab=str(tmp_path / "absent.keytab"),
        )
        user = await Session.user(layer, "f-keytab", Session.local())
        row = service_pg.model_copy(update={"auth": missing})
        await store.grant(await store.add("main", row), GrantTarget.user(UUID(user.id)))
        Session.enter(user, Session.local())

        result = await Guarded.failure(
            Guarded.pg(raw_config, store, None), connection_name="main"
        )

        _expect(result, "KeytabError", "absent.keytab")

    async def test_web_row_without_base_url_covers_no_host(
        self, raw_config, store, layer
    ) -> None:
        user = await Session.user(layer, "f-web", Session.local())
        row = HttpProfile(ssl_verify=False)
        await store.grant(await store.add("blank", row), GrantTarget.user(UUID(user.id)))
        Session.enter(user, Session.local())

        result = await Guarded.failure(
            Guarded.web(raw_config, store),
            url="https://example.com/",
            connection_name="blank",
        )

        _expect(
            result,
            ConnectionRefusal.HOST_NOT_ALLOWED,
            "outside connection 'blank'",
        )

    async def test_web_url_outside_the_connection_host(
        self, raw_config, store, layer
    ) -> None:
        user = await Session.user(layer, "f-web-host", Session.local())
        row = HttpProfile(base_url="https://*.example.com", ssl_verify=False)
        await store.grant(await store.add("lab", row), GrantTarget.user(UUID(user.id)))
        Session.enter(user, Session.local())

        result = await Guarded.failure(
            Guarded.web(raw_config, store),
            url="https://example.com/",
            connection_name="lab",
        )

        _expect(
            result,
            ConnectionRefusal.HOST_NOT_ALLOWED,
            "outside connection 'lab'",
            "*.example.com",
        )


class TestStartup:
    """Конфигурация, при которой приложение не должно подняться."""

    async def test_pg_tools_without_connections_section(
        self, raw_config: DictConfig
    ) -> None:
        disabled = OmegaConf.create(OmegaConf.to_container(raw_config, resolve=False))
        OmegaConf.update(disabled, "connections.enable", False)
        # остальные секции гасятся: проверяется только отказ pg без [connections]
        for name in OmegaConf.select(disabled, "tool"):
            if name != "pg":
                OmegaConf.update(disabled, f"tool.{name}.enable", False)

        try:
            with pytest.raises(RuntimeError, match=r"\[connections\] enable = true"):
                ChatPlugins.load(disabled, StubRefs.of(lambda: None, lambda: None))  # type: ignore[arg-type]
        finally:
            ZygoteRegistry.stop_all()
