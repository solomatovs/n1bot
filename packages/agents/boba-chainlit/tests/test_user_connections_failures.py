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

import pytest
from chainlit.user import PersistedUser
from chainlit.user import User as ChainlitUser
from langchain_core.tools import StructuredTool
from omegaconf import DictConfig, OmegaConf
from psycopg import sql
from psycopg.types.json import Jsonb
from pydantic import SecretStr, create_model

from boba.chainlit.agent.toolrun.errors import ToolErrorGuard
from boba.chainlit.agent.toolrun.injected import InjectedConfig
from boba.chainlit.auth.kerberos import KerberosAuth
from boba.chainlit.connections import (
    ConnectionKind,
    ConnectionsConfig,
    ConnectionStore,
    GrantTarget,
)
from boba.chainlit.connections.whitelist import ConnectionKeying
from boba.chainlit.data.data_layer import PostgresDataLayer
from boba.chainlit.domain.session import UserMetadataField
from boba.chainlit.infra.plugins import load_tools
from boba.chainlit.infra.user_connections import (
    ConnectionRefusal,
    SsoLogin,
    UserConnections,
    UserConnectionsSpec,
)
from boba.db.postgres import AsyncPostgresPool, PostgresConfig
from boba.krb import (
    CcacheRegistry,
    DelegatedConfig,
    DelegationMode,
    KeytabConfig,
    KeytabCredentials,
    UserCcache,
)
from boba.sandbox.zygote import ZygoteRegistry
from boba.settings import bind
from boba.tool.pg.tools import PgToolConfig
from boba.tool.web.tools import WebGrepConfig
from boba.toolkit.facade import Injected
from boba.toolkit.result import ErrorResult, ToolArtifact
from boba.transport.http import HttpProfile

pytestmark = pytest.mark.anyio

_KRB = Path(__file__).resolve().parents[4] / "compose" / "conf" / "krb"
KRB5_CONF = _KRB / "krb5.conf"
SERVICE_KEYTAB = _KRB / "boba-svc.keytab"
SERVICE_PRINCIPAL = "boba-svc@LOSHARA.COM"
LOGIN = "login-failures"

live_kdc = pytest.mark.skipif(
    not SERVICE_KEYTAB.is_file() or not KRB5_CONF.is_file(),
    reason="нет keytab/krb5.conf локального AD",
)

SCHEMA = "connections_failures"
ROLE = "analyst"


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
    return service_pg.model_copy(update={"kerberos": DelegatedConfig()})


class Registries:
    """Реестры делегированных тикетов под разные беды."""

    @staticmethod
    def healthy(tmp_path: Path) -> CcacheRegistry:
        ccache = f"FILE:{tmp_path / 'tgt'}"
        KeytabCredentials.of(
            KeytabConfig(
                keytab=str(SERVICE_KEYTAB),
                principal=SERVICE_PRINCIPAL,
                ccache=ccache,
                krb5_config=str(KRB5_CONF),
            )
        ).ensure()

        built = CcacheRegistry(

            mode=DelegationMode.FORWARDED,

            renew=False,

            krb5_config=str(KRB5_CONF),

        )
        built.register(UserCcache(SERVICE_PRINCIPAL, ccache, LOGIN))
        return built

    @staticmethod
    def expired(tmp_path: Path) -> CcacheRegistry:
        """Ccache входа без годного тикета: так выглядит истёкший TGT."""
        path = tmp_path / "stale"
        path.write_bytes(b"not a ccache")

        built = CcacheRegistry(

            mode=DelegationMode.FORWARDED,

            renew=False,

            krb5_config=str(KRB5_CONF),

        )
        built.register(UserCcache(SERVICE_PRINCIPAL, f"FILE:{path}", LOGIN))
        return built

    @staticmethod
    def dead_kdc(tmp_path: Path) -> CcacheRegistry:
        """Годный TGT, но krb5.conf смотрит на мёртвый KDC."""
        ccache = f"FILE:{tmp_path / 'tgt'}"
        KeytabCredentials.of(
            KeytabConfig(
                keytab=str(SERVICE_KEYTAB),
                principal=SERVICE_PRINCIPAL,
                ccache=ccache,
                krb5_config=str(KRB5_CONF),
            )
        ).ensure()

        conf = tmp_path / "krb5-dead.conf"
        conf.write_text(Registries._dead_kdc_conf())

        built = CcacheRegistry(

            mode=DelegationMode.FORWARDED,

            renew=False,

            krb5_config=str(conf),

        )
        built.register(UserCcache(SERVICE_PRINCIPAL, ccache, LOGIN))
        return built

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
    def sso(principal: str, login: str) -> dict[str, object]:
        return {
            UserMetadataField.ROLES: [ROLE],
            UserMetadataField.PROVIDER: KerberosAuth.__name__,
            UserMetadataField.PRINCIPAL: principal,
            UserMetadataField.LOGIN: login,
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
        init_http_context(user=user, auth_token=token)


class Guarded:
    """Инструмент с обвязкой соединений и охранником ошибок, как в приложении."""

    @staticmethod
    def pg(raw_config: Any, store: ConnectionStore, registry: CcacheRegistry | None):
        schema = create_model(
            "GuardedPgArgs",
            connection_name=(str, ...),
            cfg=(Annotated[PgToolConfig, Injected], ...),
        )

        def resolve(name: str, annotation: Any) -> object:
            return bind(raw_config, path="tool.pg", model=PgToolConfig)

        spec = UserConnectionsSpec(ConnectionKind.POSTGRES, ConnectionKeying.NAME)
        return Guarded._build(schema, store, registry, spec, resolve)

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
    def _build(schema, store, registry, spec, resolve) -> StructuredTool:
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
            [tool], lambda: store, lambda: registry, spec, resolve
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
) -> int:
    connection_id = await store.add("main", delegated_pg)
    await store.grant(connection_id, GrantTarget.user(int(user.id)))
    return connection_id


class TestDelegationUnavailable:
    """1.1: делегированный тикет не получить."""

    async def test_sso_not_configured(
        self, raw_config, store, layer, delegated_pg, tmp_path: Path
    ) -> None:
        sso = Session.sso(SERVICE_PRINCIPAL, LOGIN)
        user = await Session.user(layer, "f-no-sso", sso)
        await _grant_delegated(store, delegated_pg, user)
        Session.enter(user, Session.sso(SERVICE_PRINCIPAL, LOGIN))

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
            Guarded.pg(raw_config, store, Registries.healthy(tmp_path)),
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
            Guarded.pg(raw_config, store, Registries.healthy(tmp_path)),
            connection_name="main",
        )

        _expect(
            result,
            ConnectionRefusal.NO_DELEGATION,
            "carried no delegated ticket",
            "Active Directory",
        )

    @live_kdc
    async def test_login_unknown_after_restart(
        self, raw_config, store, layer, delegated_pg, tmp_path: Path
    ) -> None:
        """JWT пережил рестарт приложения: тикета входа в реестре нет."""
        metadata = Session.sso(SERVICE_PRINCIPAL, "login-before-restart")
        user = await Session.user(layer, "f-restart", metadata)
        await _grant_delegated(store, delegated_pg, user)
        Session.enter(user, metadata)

        result = await Guarded.failure(
            Guarded.pg(raw_config, store, Registries.healthy(tmp_path)),
            connection_name="main",
        )

        _expect(
            result,
            ConnectionRefusal.NO_DELEGATION,
            "delegated Kerberos ticket",
            "the application restarted",
        )

    async def test_delegated_ticket_expired(
        self, raw_config, store, layer, delegated_pg, tmp_path: Path
    ) -> None:
        sso = Session.sso(SERVICE_PRINCIPAL, LOGIN)
        user = await Session.user(layer, "f-expired", sso)
        await _grant_delegated(store, delegated_pg, user)
        Session.enter(user, Session.sso(SERVICE_PRINCIPAL, LOGIN))

        result = await Guarded.failure(
            Guarded.pg(raw_config, store, Registries.expired(tmp_path)),
            connection_name="main",
        )

        _expect(result, "CredentialsExpiredError", "expired", "sign in again")

    @live_kdc
    async def test_kdc_unreachable(
        self, raw_config, store, layer, delegated_pg, tmp_path: Path
    ) -> None:
        sso = Session.sso(SERVICE_PRINCIPAL, LOGIN)
        user = await Session.user(layer, "f-kdc", sso)
        await _grant_delegated(store, delegated_pg, user)
        Session.enter(user, Session.sso(SERVICE_PRINCIPAL, LOGIN))

        result = await Guarded.failure(
            Guarded.pg(raw_config, store, Registries.dead_kdc(tmp_path)),
            connection_name="main",
        )

        _expect(result, "KerberosError", "ticket to")

    @live_kdc
    async def test_service_unknown_to_kdc(
        self, raw_config, store, layer, delegated_pg, tmp_path: Path
    ) -> None:
        nowhere = delegated_pg.model_copy(update={"host": "nowhere.loshara.com"})
        sso = Session.sso(SERVICE_PRINCIPAL, LOGIN)
        user = await Session.user(layer, "f-spn", sso)
        await _grant_delegated(store, nowhere, user)
        Session.enter(user, Session.sso(SERVICE_PRINCIPAL, LOGIN))

        result = await Guarded.failure(
            Guarded.pg(raw_config, store, Registries.healthy(tmp_path)),
            connection_name="main",
        )

        _expect(result, "KerberosError", "nowhere.loshara.com")

    @live_kdc
    async def test_ticket_too_short_for_the_row(
        self, raw_config, store, layer, service_pg, tmp_path: Path
    ) -> None:
        strict = service_pg.model_copy(
            update={"kerberos": DelegatedConfig(min_lifetime=10**9)}
        )
        sso = Session.sso(SERVICE_PRINCIPAL, LOGIN)
        user = await Session.user(layer, "f-short", sso)
        await _grant_delegated(store, strict, user)
        Session.enter(user, Session.sso(SERVICE_PRINCIPAL, LOGIN))

        result = await Guarded.failure(
            Guarded.pg(raw_config, store, Registries.healthy(tmp_path)),
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
            Guarded.pg(raw_config, store, Registries.healthy(tmp_path)),
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
            Guarded.pg(raw_config, store, Registries.healthy(tmp_path)),
            connection_name="main",
        )

        for phrase in ("LocalAuth", "Kerberos SSO button", SsoLogin.RETRY_HINT):
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
                    "insert into {} (name, kind, data) values (%s, %s, %s) returning id"
                ).format(sql.Identifier(SCHEMA, "connections")),
                ("broken", "postgres", Jsonb({"kind": "postgres"})),
            )
            row = await cur.fetchone()
        if row is None:
            raise AssertionError("row must be inserted")
        await store.grant(int(row[0]), GrantTarget.user(int(user.id)))
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
                "password": SecretStr("s3cret"),
                "kerberos": None,
                "gssencmode": "disable",
            }
        )
        connection_id = await store.add("main", secret)
        await store.grant(connection_id, GrantTarget.user(int(user.id)))
        Session.enter(user, Session.local())

        cfg = ConnectionsConfig(enable=True, db_schema=SCHEMA, encryption_key=_key())
        foreign = ConnectionStore(cfg, pool)

        result = await Guarded.failure(
            Guarded.pg(raw_config, foreign, None), connection_name="main"
        )

        _expect(result, "SecretCryptoError", "not decrypted")

    @pytest.mark.parametrize(
        "section",
        [
            {
                "keytab": str(SERVICE_KEYTAB),
                "principal": SERVICE_PRINCIPAL,
                "ccache": "MEMORY:stolen",
            },
            {"kind": "ccache", "principal": SERVICE_PRINCIPAL, "ccache": "FILE:/x"},
        ],
        ids=["keytab-with-process-ccache", "host-ccache-section"],
    )
    async def test_row_with_foreign_ccache_is_not_a_profile(
        self, raw_config, store, layer, pool: AsyncPostgresPool, section
    ) -> None:
        """Строка, тянущая процессный или хостовый ccache, профилем не считается."""
        user = await Session.user(layer, "f-foreign-ccache", Session.local())
        data = {
            "kind": "postgres",
            "host": "h",
            "user": "u",
            "dbname": "d",
            "gssencmode": "require",
            "connect_timeout": 5,
            "kerberos": section,
        }
        async with pool.cursor() as cur:
            await cur.execute(
                sql.SQL(
                    "insert into {} (name, kind, data) values (%s, %s, %s) returning id"
                ).format(sql.Identifier(SCHEMA, "connections")),
                ("main", "postgres", Jsonb(data)),
            )
            row = await cur.fetchone()
        if row is None:
            raise AssertionError("row must be inserted")
        await store.grant(int(row[0]), GrantTarget.user(int(user.id)))
        Session.enter(user, Session.local())

        result = await Guarded.failure(
            Guarded.pg(raw_config, store, None), connection_name="main"
        )

        _expect(result, "ConnectionStoreError", "not a valid connection profile")

    async def test_keytab_file_missing(
        self, raw_config, store, layer, service_pg, tmp_path: Path
    ) -> None:
        missing = KeytabConfig(
            keytab=str(tmp_path / "absent.keytab"),
            principal=SERVICE_PRINCIPAL,
            ccache=f"FILE:{tmp_path / 'cc'}",
            krb5_config=str(KRB5_CONF),
        )
        user = await Session.user(layer, "f-keytab", Session.local())
        row = service_pg.model_copy(update={"kerberos": missing})
        await store.grant(await store.add("main", row), GrantTarget.user(int(user.id)))
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
        await store.grant(await store.add("blank", row), GrantTarget.user(int(user.id)))
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
        row = HttpProfile(base_url="https://*.loshara.com", ssl_verify=False)
        await store.grant(await store.add("lab", row), GrantTarget.user(int(user.id)))
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
            "*.loshara.com",
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
                load_tools(disabled, lambda: None, lambda: None)  # type: ignore[arg-type]
        finally:
            ZygoteRegistry.stop_all()
