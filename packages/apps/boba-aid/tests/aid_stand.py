"""Общие фикстуры тестов aid: слой данных, сессии, стенды сервиса инструментов."""

import asyncio
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import tomllib
from collections.abc import (
    AsyncIterator,
    Collection,
    Iterator,
    Mapping,
    Sequence,
)
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from types import SimpleNamespace
from typing import Any, ClassVar
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

import mcp_types as mt
import pytest
from acp.experimental.v2 import schema
from chainlit.context import ChainlitContext
from chainlit.session import WebsocketSession
from chainlit.step import Step, StepDict
from chainlit.user import PersistedUser
from chainlit.user import User as ChainlitUser
from psycopg import sql
from pydantic import SecretStr

from boba.access.grants import GrantSubject, GrantTarget
from boba.aid.agent.commands import CommandMenus
from boba.aid.agent.relay import (
    McpRegistration,
    McpRegistrations,
    McpRelay,
)
from boba.aid.auth.claims import SessionClaims, TokenReader
from boba.aid.auth.tokens import JwtTokens
from boba.aid.canvas.remote import RemoteJournals, RemoteStreams
from boba.aid.canvas.tools import CanvasOpener
from boba.aid.chat.ui_text import UiText
from boba.aid.data.data_layer import PostgresDataLayer
from boba.aid.data.feedbacks import FeedbacksTable
from boba.aid.data.threads import ThreadsTable
from boba.aid.data.users import UsersTable
from boba.aid.domain.keys import AppPrefix
from boba.aid.infra.config import AppConfig
from boba.aid.infra.session import (
    ChainlitSession,
    ChainlitSessions,
    OwnerSessions,
    current_session,
)
from boba.aid.infra.thread_room import ThreadSurfaces
from boba.aid.mcp_servers import McpAppError, McpOrigin
from boba.aid.mcp_servers.catalog import McpConnectionCatalogs
from boba.aid.mcp_servers.sources import McpViewer, ServerRecord, ServerSource
from boba.aid.mcp_servers.store import StoredServer
from boba.aid.rendering.chat_view import ChatSink, ChatView, StepRole
from boba.aid.rendering.renderer import NoSurface, RenderSurface, TabSurfaces
from boba.aid.rendering.tool import AppMeta, AppSource, AppUnread, ToolSource
from boba.auth.providers.backends import KerberosSessionTickets
from boba.cancellation import RunCancellation
from boba.canvas.canvas import CanvasSignal, SignalTransport
from boba.canvas.journal import StreamSlice
from boba.canvas.keys import WorkspaceMount
from boba.config import bind
from boba.connections.manifest import ConnectionTypes
from boba.core.mcp_names import McpToolNames
from boba.crypto import SecretCipher
from boba.db.postgres import AsyncPostgresPool
from boba.db.postgres.connection import PostgresConfig
from boba.identity.api import StoredUser, UserRows
from boba.identity.authentication import (
    AccessGrant,
    AuthenticatedPrincipal,
    AuthenticatedSession,
    RenewalKind,
)
from boba.identity.context import (
    CallContext,
    ChatInitiator,
    CredentialAbsence,
    DelegatedTicket,
    NoUserCredential,
    Scope,
    Subject,
)
from boba.identity.errors import InternalServiceError
from boba.identity.lifetime import SessionLifetime
from boba.identity.locks import MemoryLiveLocks
from boba.identity.principal import UserIdentity
from boba.identity.session import Login
from boba.kerberos import DelegationMode, SignInTicket
from boba.krb.seal import SsoTickets, TicketSealer
from boba.mcp_client.client import (
    BearerAuth,
    FixedServerConfigs,
    McpCaller,
    McpDescription,
    McpServerConfig,
    McpServers,
    ServerConfigs,
)
from boba.mcp_server.app import McpAppConfig
from boba.mcp_server.auth import ServiceMark
from boba.mcp_server.connections.store import ConnectionStore
from boba.mcp_server.connections.tools import ConnectionTools
from boba.mcp_server.migrations import configured_migrations
from boba.messaging import MemoryMessageBus, MemoryPayloadStore
from boba.oauth.server.tokens import ServiceTokens, TokenHolder
from boba.stand.messaging import memory_message_bus
from boba.stand.migrate import StandMigrations, StandSchema
from boba.stand.refs import StandRefs
from boba.stand.roles import RoleSeed
from boba.stand.service_signin import ServiceSignIn
from boba.stand.site import StandConfigEnv, StandConfigPaths
from boba.stand.storage import StorageSeed
from boba.stand.ui.database import run_blocking
from boba.stand.ui.stand import ServiceStand, StandPorts, StandProcess
from boba.stand.ui.toml_text import TomlText
from boba.stand.zygote import ROOTFS_IMAGE, SandboxCgroup
from boba.stand_core.context import CallStand, StandIdentity
from boba.stand_core.context import FakeTurn as FakeTurn
from boba.stand_core.fakes import FakeSecret as FakeSecret
from boba.stand_core.fakes import FakeUrl as FakeUrl
from boba.storage.client import LocalStorageClient, LocalStorageConfig, StorageClient
from boba.toolkit.channels import JournalChannel
from boba.toolkit.dag import (
    JournalAddress,
)
from boba.toolkit.mcp import McpResults
from boba.toolkit.result import ToolResultBase
from boba.transport.acp.protocol import (
    McpErrorOutcome,
    McpMessage,
    McpVersion,
    ToolSourceContent,
)


class AuthTestClaim(StrEnum):
    """Names accepted by test helpers when they deliberately mint signed claims."""

    ROLES = "roles"
    PROFILES = "profiles"
    PROFILE = "profile"
    PROVIDER = "provider"
    PRINCIPAL = "principal"
    TICKET = "sso_ticket"
    CREDENTIAL_REASON = "credential_reason"
    GENERATION = "generation"
    IDENTITY = "identity"
    USER_ID = "user_id"
    CREATED_AT = "created_at"


from boba.workspace.launcher import ReadWindow

AUTH_USER = "corporate:test-user"


class MirroredUserConnectionStore(ConnectionStore):
    """MCP store for tests with AID identities copied before a user grant.

    Real MCP sign-in creates its local users row. Direct store tests model that
    boundary by copying the same UUID and confirmed identity from AID first.
    """

    def __init__(
        self,
        pool: AsyncPostgresPool,
        schema: str,
        cipher: SecretCipher,
        source_users: UserRows,
    ) -> None:
        super().__init__(pool, schema, cipher, ConnectionTypes.discover())
        self._stand_pool = pool
        self._source_users = source_users

    async def grant(self, connection_id: UUID, target: GrantTarget) -> UUID:
        if target.kind is GrantSubject.USERS:
            user = await self._source_users.stored_by_id(target.id)
            if user is not None:
                statement = sql.SQL(
                    "insert into {}.users (id, realm, login) "
                    "values (%s, %s, %s) on conflict (id) do nothing"
                ).format(sql.Identifier(self.schema))
                async with self._stand_pool.connection() as conn:
                    await conn.execute(
                        statement,
                        (user.id, user.identity.realm, user.identity.login),
                    )

        return await super().grant(connection_id, target)


class TabSession:
    """Сессия вкладки в объёме, который читает регистрация сервера: объект с
    токеном входа, который обновление сессии заменяет на месте."""

    LOGIN: ClassVar[str] = "corporate:ivanov"
    PRINCIPAL: ClassVar[str] = "ivanov@STAND.LOCAL"
    SERVER: ClassVar[str] = "aid_all"

    def __init__(self) -> None:
        self.raw = SimpleNamespace(token="")
        self.live = ChainlitSession(self.raw, StandTokens())

    def sign_in(self, roles: frozenset[str], sealed_ticket: str) -> None:
        """Кладёт в сессию токен входа kerberos с ролями и билетом — так же
        токен заменяет обновление сессии."""
        self._set_user(roles, self.PRINCIPAL, sealed_ticket)

    def password_sign_in(self, roles: frozenset[str]) -> None:
        self._set_user(roles)

    def _set_user(
        self,
        roles: frozenset[str],
        principal: str = "",
        sealed_ticket: str = "",
    ) -> None:
        identity = UserIdentity.model_validate(self.LOGIN)
        user_id = uuid5(NAMESPACE_URL, f"boba-aid-stand:{identity.identifier}")
        self.raw.user = PersistedUser(
            id=str(user_id),
            identifier=identity.identifier,
            createdAt=StandTokens.CREATED_AT.isoformat(),
            metadata={},
        )
        session = StandTokens.session(
            identity.identifier,
            user_id,
            roles=roles,
            profiles={self.SERVER},
            principal=principal,
            sealed_ticket=sealed_ticket,
        )
        self.raw.token = StandTokens.issue(session)

    def registration(self, user_id: UUID, profile: str) -> McpRegistration:
        return McpRegistration(
            user_id=user_id,
            login=self.LOGIN,
            thread_id=str(uuid4()),
            profile=profile,
            session=self.live,
            server=self.SERVER,
        )


class ServiceProcess:
    """Стенд сервиса boba-mcp отдельным процессом; вход — proxy."""

    STAND: ClassVar[Path] = (
        Path(__file__).resolve().parents[4]
        / "packages/apps/boba-mcp/tests/service_stand.py"
    )
    SECRET: ClassVar[str] = "stand-proxy-secret"

    OWNER: ClassVar[str] = str(uuid5(NAMESPACE_URL, "boba-user:corporate:alice"))
    """Под этим id сервис держит журналы и файлы пользователя alice, чьим
    токеном входит token_servers()."""

    def __init__(self, workdir: Path) -> None:
        self.journal_dir = workdir / "journal"
        """Каталог журналов вызовов стенда сервиса."""
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            self.port = int(probe.getsockname()[1])

        self._process = subprocess.Popen(
            [sys.executable, str(self.STAND), str(self.port), str(workdir)]
        )
        self._public = f"http://127.0.0.1:{self.port}"
        self._sign_in = ServiceSignIn(
            self._public, "stand-chat", "stand-client-secret", self.SECRET
        )

    def await_listening(self) -> None:
        deadline = time.monotonic() + 60.0
        while time.monotonic() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", self.port), timeout=1.0):
                    return
            except OSError:
                time.sleep(0.2)

        raise AssertionError(f"the service did not listen on port {self.port}")

    def servers(self, path: str = "/service") -> FixedServerConfigs:
        service = {"scheme": "http", "host": "127.0.0.1", "port": self.port, "path": ""}
        endpoint = {
            "transport": "streamable-http",
            "service": service,
            "path": path,
            "auth": {
                "auth": "proxy",
                "issuer": service,
                "client_id": "stand-chat",
                "client_secret": "stand-client-secret",
                "secret": self.SECRET,
            },
        }
        server = {
            "endpoint": endpoint,
            "limits": {"connect_timeout_sec": 30.0, "call_timeout_sec": 60.0},
        }

        return FixedServerConfigs.model_validate({"servers": {"boba": server}})

    def token_servers(self, name: str) -> FixedServerConfigs:
        """Секция [mcp.servers] с одним сервером name: вход токеном
        пользователя alice с ролью dev."""
        path = "/service"
        token = self._sign_in.token("alice", "dev", f"{self._public}{path}")
        server = {
            "endpoint": {
                "transport": "streamable-http",
                "service": {
                    "scheme": "http",
                    "host": "127.0.0.1",
                    "port": self.port,
                    "path": "",
                },
                "path": path,
                "auth": {"auth": "bearer", "token": token},
            },
            "limits": {"connect_timeout_sec": 30.0, "call_timeout_sec": 60.0},
        }

        return FixedServerConfigs.model_validate({"servers": {name: server}})

    def stop(self) -> None:
        self._process.terminate()
        self._process.wait(timeout=10)


class ToolServiceConfig(ServiceStand):
    """Сервер тестов credentials: каталог MCP и открыватель SSO-билетов MCP."""

    def write(self) -> None:
        super().write()
        document = tomllib.loads(self.files.overlay.read_text())
        document["app"]["auth"].append("${auth.kerberos}")
        self.files.overlay.write_text(TomlText.dumps(document))


class ToolService(ServerConfigs):
    """Настоящий MCP с плагинами в песочнице, каталогом и тестовым издателем JWT.

    Тесты создают profiles и grants в его схеме и SSO-билеты его ключом.
    Полный HTTP OAuth-вход отдельно проверяет test_stored_sso."""

    ENDPOINT: ClassVar[str] = "all"
    PLUGINS: ClassVar[tuple[str, ...]] = ("pg", "ch", "web")
    """Плагины песочницы, которые зовут тесты сервиса: соединения
    пользователя (pg, ch) и делегированный HTTP (web)."""
    BOOT_TIMEOUT_SEC: ClassVar[float] = 300.0

    def __init__(self, workdir: Path, db_name: str) -> None:
        self._config = ToolServiceConfig(
            workdir=workdir,
            app_port=StandPorts().free(),
            db_name=db_name,
            sandbox=True,
            plugins=self.PLUGINS,
            db_schema=f"mcp_tools_{uuid4().hex[:12]}",
            generation="connection-tools",
        )
        self._process = StandProcess[ServiceStand](
            config=self._config, log_path=workdir / "mcp.log"
        )
        self._storage = StandSchema(
            self._config.files.paths(self._config.app),
            "boba.mcp_server",
            configured_migrations(),
        )

    @classmethod
    def missing(cls) -> str:
        """Чего сервису не хватает для песочницы на этом хосте; пустая
        строка — всё на месте."""
        if shutil.which("bwrap") is None:
            return "bwrap is not on PATH (build: make -C build build APP=runtime)"

        if not ROOTFS_IMAGE.exists():
            return (
                f"plugin rootfs {ROOTFS_IMAGE} is not built "
                "(make -C build build APP=runtime)"
            )

        return SandboxCgroup().missing()

    @property
    def server_name(self) -> str:
        return self._config.server_name(self.ENDPOINT)

    def start(self) -> None:
        self._config.write()
        run_blocking(self._storage.create())
        self._process.start(self.BOOT_TIMEOUT_SEC)
        self._app = bind(
            self._config.app.config_files()
            .loader(self._config.files.paths(self._config.app))
            .load(),
            "app",
            McpAppConfig,
        )
        session = self._app.session
        self._tokens = ServiceTokens(
            self._app.server.public_url,
            session.auth_secret,
            session.lifetime(),
            session.session_generation(),
            ServiceMark.TOKEN_SALT,
        )
        self._signed: dict[str, McpServerConfig] = {}

    def stop(self) -> None:
        self._process.stop()
        run_blocking(self._storage.drop())

    def servers(self) -> FixedServerConfigs:
        """Секция [mcp.servers] чата с одним сервером — этим сервисом."""
        servers = {self.server_name: self._config.server(self.ENDPOINT)}

        return FixedServerConfigs.model_validate({"servers": servers})

    async def catalog(self, pool: AsyncPostgresPool) -> ConnectionStore:
        """Очищает только каталог изолированного сервера перед тестом."""
        schema = self._app.db_schema
        async with pool.connection() as conn:
            await conn.execute(
                sql.SQL("delete from {}.grants").format(sql.Identifier(schema))
            )
            await conn.execute(
                sql.SQL("delete from {}.connections").format(sql.Identifier(schema))
            )
            await conn.execute(
                sql.SQL("delete from {}.users").format(sql.Identifier(schema))
            )
        self._signed.clear()

        cipher = SecretCipher(self._app.connection_store.cipher.get_secret_value())
        return ConnectionStore(pool, schema, cipher, ConnectionTypes.discover())

    def tickets(self) -> SsoTickets:
        tickets = KerberosSessionTickets().build(
            self._app.auth, self._app.session.auth_secret
        )
        assert tickets is not None, "the MCP test service must open SSO tickets"
        return tickets

    async def config_of(self, name: str, caller: McpCaller) -> McpServerConfig | None:
        if name != self.server_name:
            return None

        session = current_session()
        cached = self._signed.get(session.token)
        if cached is not None:
            return cached

        authenticated = session.authenticated_session()
        principal = authenticated.principal.model_copy(
            update={
                "access": authenticated.principal.access.model_copy(
                    update={"profiles": frozenset({self.ENDPOINT})}
                )
            }
        )
        authenticated = authenticated.model_copy(update={"principal": principal})
        assert session.user_id is not None
        resource = f"{self._app.server.public_url}/{self.ENDPOINT}"
        pool = AsyncPostgresPool(self._app.postgres)
        await pool.open()
        try:
            users = UsersTable(pool, self._app.db_schema)
            await RoleSeed(users).user(
                UUID(session.user_id),
                principal.identity,
                principal.roles,
            )
        finally:
            await pool.close()
        claims = session.signed_in()
        holder = TokenHolder(
            principal=principal,
            credential=authenticated.credential,
            started=claims.auth_time,
            resource=resource,
        )
        token = self._tokens.access(holder, self._config.CLIENT_ID).access_token
        configured = self.servers().servers[name]
        endpoint = configured.endpoint.model_copy(
            update={"auth": BearerAuth(token=SecretStr(token))}
        )
        configured = configured.model_copy(update={"endpoint": endpoint})
        self._signed[session.token] = configured
        return configured

    def tail(self) -> str:
        return self._process.tail()


class RelayStand:
    """Ретранслятор AID без локального каталога connections."""

    ROLE: ClassVar[str] = "dev"
    VERSION: ClassVar[str] = "0.0"
    """Версия чата стенда в serverInfo ретранслятора и сервера chat."""

    def __init__(self) -> None:
        self.registrations = McpRegistrations()
        self.canvas = CanvasOpener(ThreadSurfaces())
        self.bus = memory_message_bus("relay-stand")

    def relay(self, servers: McpServers) -> McpRelay:
        return self.relay_with(servers, self.canvas)

    def menus(
        self, servers: McpServers, source: ServerSource | None = None
    ) -> CommandMenus:
        """Реестры команд вкладок над ретранслятором серверов servers и
        каталогами серверов из API."""
        if source is None:
            source = FixedServers({})
        return CommandMenus(self.relay(servers), McpConnectionCatalogs(servers, source))

    def relay_with(self, servers: McpServers, canvas: CanvasOpener) -> McpRelay:
        """Ретранслятор стенда с указанной панелью."""
        return McpRelay(
            self.registrations,
            servers,
            canvas,
            self.bus,
            self.VERSION,
        )


class HolderSurface(NoSurface, TabSurfaces):
    """Поверхность держателя хода в тесте: контекст chainlit HTTP-сессии
    пользователя треда, в котором открывается панель и пишутся элементы;
    живая вкладка любого треда — она же."""

    def __init__(self, context: ChainlitContext) -> None:
        self._context = context

    def context(self) -> ChainlitContext | None:
        return self._context

    def anchor(self) -> WebsocketSession:
        msg = "the test holder surface has no websocket session to anchor on"
        raise InternalServiceError(internal_detail=msg, user_detail=None)

    def thread(self, anchor: WebsocketSession, thread_id: str) -> RenderSurface:
        return self

    def live(self, thread_id: str) -> RenderSurface:
        return self


class ServiceTool:
    """Инструмент сервиса глазами теста: вызов уходит ретранслятором чата от
    имени пользователя текущей сессии, ответ — результат семейства.

    Создаётся ServiceTools под именем инструмента на сервере; интерфейс run
    тот же, что у инструмента хоста, поэтому вызов разбирает
    boba.stand.toolsetup.Call.
    """

    def __init__(self, name: str, tools: "ServiceTools") -> None:
        self.name = name
        self._tools = tools

    async def run(self, call_id: str, args: Mapping[str, Any]) -> ToolResultBase:
        return await self._tools.call(self.name, call_id, args)


class ServiceTools:
    """Тестовый клиент MCP через ретранслятор AID с именами connections.

    ToolService выпускает bearer-токен MCP по личности тестового входа."""

    def __init__(self, service: ToolService) -> None:
        self._service = service
        self._names = McpToolNames()
        self._servers = McpServers(service)
        self._stand = RelayStand()
        self._relay = self._stand.relay(self._servers)
        self._results = McpResults()
        self._decoder = json.JSONDecoder()
        self._client = McpServers.OWN
        self._heard: asyncio.Queue[McpMessage] = asyncio.Queue()

    async def stop(self) -> None:
        await self._servers.stop()

    def rows(self, result: ToolResultBase) -> list[dict[str, Any]]:
        """Строки таблицы или выборки из текста итога для модели: он
        начинается JSON-списком строк."""
        rows, _ = self._decoder.raw_decode(result.llm_view())

        return rows

    def __getitem__(self, name: str) -> ServiceTool:
        """Инструмент сервиса по имени на сервере."""
        return ServiceTool(name, self)

    async def call(
        self, tool: str, call_id: str, args: Mapping[str, Any]
    ) -> ToolResultBase:
        """Вызов tools/call ретранслятором от имени пользователя текущей
        сессии chainlit: тред сессии — область вызова."""
        session = current_session()
        user_id = session.user_id
        if user_id is None:
            msg = f"tool {tool!r}: the chainlit session of the test has no user"
            raise AssertionError(msg)

        thread_id = session.thread_id
        if thread_id is None:
            msg = f"tool {tool!r}: the chainlit session of the test has no thread"
            raise AssertionError(msg)

        profile = session.chat_profile
        if profile is None:
            msg = f"tool {tool!r}: the chainlit session of the test has no profile"
            raise AssertionError(msg)

        server = self._service.server_name
        registration = McpRegistration(
            user_id=UUID(user_id),
            login=session.identifier,
            thread_id=thread_id,
            profile=profile,
            session=session,
            server=server,
        )
        registrations = self._stand.registrations
        registered = registrations.register(registration, [server])[0]
        envelope = {
            mt.PROTOCOL_VERSION_META_KEY: McpVersion.V2026_07_28.value,
            mt.CLIENT_CAPABILITIES_META_KEY: self._client.capabilities.model_dump(
                mode="json", by_alias=True, exclude_none=True
            ),
        }
        message = McpMessage(
            server_id=registered.server_id,
            request_id=call_id,
            method="tools/call",
            params={"name": tool, "arguments": dict(args), "_meta": envelope},
        )
        try:
            outcome = await self._relay.answer(message, self._heard.put)
        finally:
            registrations.revoke(registered.server_id)

        if isinstance(outcome, McpErrorOutcome):
            msg = f"tool {tool!r} of {server!r} answered an MCP error: {outcome.error}"
            raise AssertionError(msg)

        reply = mt.CallToolResult.model_validate(outcome.result)
        full = self._names.full(server, tool)

        return self._results.result(full, call_id, reply)


@pytest.fixture(scope="module")
def service_process(
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[ServiceProcess]:
    """Стенд boba-mcp модуля отдельным процессом: вход proxy, инструменты-заглушки."""
    process = ServiceProcess(tmp_path_factory.mktemp("boba-mcp"))
    try:
        process.await_listening()
        yield process
    finally:
        process.stop()


@pytest.fixture(scope="module")
def tool_service(
    tmp_path_factory: pytest.TempPathFactory, test_database: str
) -> Iterator[ToolService]:
    """Сервис инструментов модуля: настоящий boba-mcp с плагинами в песочнице."""
    process = ToolService(tmp_path_factory.mktemp("boba-mcp"), test_database)
    try:
        process.start()
        yield process
    finally:
        process.stop()


class NoTabs(OwnerSessions):
    """Источник сессий без живых вкладок: владелец файла ищется по строке users."""

    def of_user(self, user_id: UUID) -> Sequence[ChainlitSession]:
        return ()


class OwnerRows(UserRows):
    """Строки users стенда файлов: один пользователь с ролями и профилями входа.

    Реализация UserRows только на чтение по id: её читает FileOwners, когда
    у владельца файла нет живой сессии.
    """

    def __init__(self, owner: StoredUser) -> None:
        self._owner = owner

    async def stored(self, identifier: Login) -> StoredUser | None:
        return None

    async def stored_by_id(self, user_id: UUID) -> StoredUser | None:
        if user_id != self._owner.id:
            return None

        return self._owner

    async def upsert(
        self,
        identifier: Login,
        identity: UserIdentity | None = None,
        roles: frozenset[str] | None = None,
    ) -> StoredUser:
        raise NotImplementedError


class FixedServers(ServerSource):
    """Серверы стендов, заданные кодом: записи видны любому зрителю и не
    меняются. Заменяет таблицу mcp_servers там, где тесту серверы не нужны
    как строки БД: у всех происхождение role, выключенных нет, правка
    отвергается отказом McpAppError."""

    def __init__(self, servers: Mapping[str, McpServerConfig]) -> None:
        self._records: list[ServerRecord] = []
        for name in sorted(servers):
            self._records.append(self._record(name, servers[name]))

    async def visible(self, viewer: McpViewer) -> Sequence[ServerRecord]:
        return list(self._records)

    async def record(self, viewer: McpViewer, name: str) -> ServerRecord:
        """Ошибки:
        McpAppError — сервера name в наборе стенда нет.
        """
        for record in self._records:
            if record.name == name:
                return record

        names: list[str] = []
        for record in self._records:
            names.append(record.name)

        msg = (
            f"the MCP servers app of {viewer.login!r} did not find the server "
            f"{name!r}: the stand has no server granted to this user, the servers "
            f"of the stand are {names or ['none']}"
        )
        raise McpAppError(msg)

    async def config_of(self, name: str, caller: McpCaller) -> McpServerConfig | None:
        for record in self._records:
            if record.name == name:
                return record.config

        return None

    async def add(
        self, viewer: McpViewer, name: str, server: StoredServer
    ) -> ServerRecord:
        raise McpAppError(f"MCP stand: cannot add server {name!r}; source is read-only")

    async def replace(
        self, viewer: McpViewer, current: str, name: str, server: StoredServer
    ) -> ServerRecord:
        raise McpAppError(
            f"MCP stand: cannot replace server {current!r}; source is read-only"
        )

    async def set_enabled(
        self, viewer: McpViewer, name: str, enabled: bool
    ) -> ServerRecord:
        raise McpAppError(
            f"MCP stand: cannot toggle server {name!r}; source is read-only"
        )

    async def remove(self, viewer: McpViewer, name: str) -> None:
        raise McpAppError(
            f"MCP stand: cannot remove server {name!r}; source is read-only"
        )

    @staticmethod
    def _record(name: str, config: McpServerConfig) -> ServerRecord:
        return ServerRecord(
            server_id=uuid5(NAMESPACE_URL, f"boba-mcp-stand:{name}"),
            name=name,
            origin=McpOrigin.ROLE,
            editable=False,
            enabled=True,
            config=config,
        )


class KnownSources:
    """Серверы тестов ленты: карточки инструментов, расширения описаний,
    ресурсы приложений и имена серверов, объявивших приложения MCP Apps,
    заданы тестом. call собирает первое уведомление вызова с источником
    (ToolSource) так, как его собрал бы ретранслятор: карточка инструмента
    (у незнакомого инструмента известного сервера — пустая схема), описание
    сервера, карточки инструментов узлов вложенного вызова и ресурс
    приложения. Вызов инструмента незнакомого сервера источника не несёт."""

    def __init__(
        self,
        tools: Mapping[str, Sequence[mt.Tool]],
        described: Mapping[str, mt.ServerCapabilities],
        apps: Collection[str],
        resources: Mapping[str, AppSource] | None = None,
    ) -> None:
        self._tools = tools
        self._described = described
        self._apps = frozenset(apps)
        self._resources: Mapping[str, AppSource] = {}
        if resources is not None:
            self._resources = resources

        self._names = McpToolNames()
        self._app_meta = AppMeta()

    def call(
        self, call_id: str, name: str, args: Mapping[str, Any]
    ) -> schema.SessionToolCallUpdate:
        """Первое уведомление вызова name с источником, если сервер знаком."""
        update = schema.SessionToolCallUpdate(
            tool_call_id=call_id,
            name=name,
            title=f"{name} {dict(args)}",
            status="pending",
            raw_input=dict(args),
        )
        source = self.source(name, args)
        if source is None:
            return update

        dumped = source.model_dump(mode="json", by_alias=True, exclude_none=True)
        content = ToolSourceContent(source=dumped).wire()

        return update.model_copy(update={"content": [content]})

    def source(self, name: str, args: Mapping[str, Any]) -> ToolSource | None:
        owner = self._names.parse(name)
        if owner is None:
            return None

        cards = self._cards(owner.server)
        capabilities = self._described.get(owner.server)
        if cards is None and capabilities is None:
            return None

        if capabilities is None:
            capabilities = mt.ServerCapabilities()

        described = mt.DiscoverResult(
            supported_versions=["2026-07-28"], capabilities=capabilities
        )
        tool = self._card(owner.server, owner.tool)

        return ToolSource(
            server=owner.server,
            tool=tool,
            description=described,
            nodes=list(self._nodes(owner.server, described, owner.tool, args)),
            app=self._app(owner.server, tool),
        )

    def _cards(self, server: str) -> Sequence[mt.Tool] | None:
        return self._tools.get(server)

    def _card(self, server: str, tool: str) -> mt.Tool:
        for known in self._tools.get(server, ()):
            if known.name == tool:
                return known

        return mt.Tool(name=tool, input_schema={"type": "object"})

    def _nodes(
        self,
        server: str,
        described: mt.DiscoverResult,
        tool: str,
        args: Mapping[str, Any],
    ) -> Iterator[mt.Tool]:
        nested = McpDescription(server, described).nested_calls()
        if nested is None:
            return

        if nested.tool != tool:
            return

        for node in nested.calls(dict(args)):
            yield self._card(server, node.tool)

    def _app(self, server: str, tool: mt.Tool) -> AppSource | None:
        if server not in self._apps:
            return None

        config = self._app_meta.of(tool)
        if config is None:
            return None

        uri = config.resource_uri
        if uri is None:
            return None

        if resource := self._resources.get(uri):
            return resource

        return AppUnread(error=f"the stand has no resource {uri}")


class PerCallStreams(RemoteStreams):
    """RemoteStreams тестов панели: на каждое чтение свой клиент MCP.

    Тесты зовут действия панели отдельными asyncio.run, а подключение
    клиента живёт в цикле событий, где открыто: общий клиент второй вызов
    не пережил бы. Реестр журналов при этом один на стенд.

    Клиент чтения не хранится в объекте: показ и сторож слежения читают
    одновременно, и общий клиент один из них закрыл бы под чтением другого.
    """

    def __init__(self, journals: RemoteJournals, caller: McpCaller) -> None:
        self._config = FixedServerConfigs(servers={})
        super().__init__(journals, self._client(), caller)

    def serve(self, config: FixedServerConfigs) -> None:
        """Серверы, с которых дальше читаются журналы."""
        self._config = config

    def _client(self) -> McpServers:
        return McpServers(self._config)

    async def slice_at(
        self, thread_id: str, call_id: str, channel: JournalChannel, offset: int
    ) -> StreamSlice | None:
        servers = self._client()
        try:
            reading = RemoteStreams(self._journals, servers, self._caller)
            return await reading.slice_at(thread_id, call_id, channel, offset)
        finally:
            await servers.stop()

    async def slice_before(
        self, thread_id: str, call_id: str, channel: JournalChannel, end: int
    ) -> StreamSlice | None:
        servers = self._client()
        try:
            reading = RemoteStreams(self._journals, servers, self._caller)
            return await reading.slice_before(thread_id, call_id, channel, end)
        finally:
            await servers.stop()


class RemoteStand:
    """Журналы вызовов MCP-серверов для тестов панели.

    Без сервиса (serve() не звали) серверов нет и любой журнал не найден.
    Тест с сервисом зовёт serve() и называет вызовы, чьи журналы лежат на
    сервере, через recorded() — так адрес журнала хранит история треда.
    """

    SERVER: ClassVar[str] = "boba"

    def __init__(self) -> None:
        self.journals = RemoteJournals()
        caller = McpCaller(login=StandIdentity.LOGIN, roles=frozenset())
        self.streams = PerCallStreams(self.journals, caller)

    def serve(self, service: ServiceProcess) -> None:
        self.streams.serve(service.token_servers(self.SERVER))

    def recorded(self, thread_id: str, call_id: str) -> None:
        """Вызов треда исполнил сервер стенда: запуск — сам тред; адрес
        журнала лента запомнила бы по итогу вызова."""
        self.journals.remember(
            thread_id,
            call_id,
            JournalAddress(server=self.SERVER, run=thread_id, call=call_id),
        )

    def forget(self) -> None:
        """Сброс между тестами: запомненных адресов нет."""
        self.journals = RemoteJournals()
        self.streams = PerCallStreams(
            self.journals, McpCaller(login=StandIdentity.LOGIN, roles=frozenset())
        )


@dataclass
class Seed:
    """Базовые данные под тест: слой, пользователь и тред."""

    layer: PostgresDataLayer
    user: PersistedUser
    thread_id: str
    answer_step_id: str
    """id шага итогового ответа — он же цель для feedback и вложений."""


@pytest.fixture(autouse=True)
def workspace_mount() -> None:
    """Точку рабочего каталога в приложении ставит загрузчик из профиля."""
    WorkspaceMount.configure("/workspace")


class AppLanguage:
    """Язык интерфейса приложения пакета — [UI].language конфига chainlit в
    assets: на нём говорит стенд aid, запущенный отдельным процессом."""

    ASSETS: ClassVar[Path] = Path(__file__).resolve().parents[1] / "assets"
    CONFIG: ClassVar[tuple[str, str]] = (".chainlit", "config.toml")

    def language(self) -> str:
        config = self.ASSETS.joinpath(*self.CONFIG)
        doc = tomllib.loads(config.read_text(encoding="utf-8"))
        return str(doc["UI"]["language"])

    def texts(self) -> UiText:
        return UiText(self.ASSETS, self.language())


@pytest.fixture(scope="session")
def app_texts() -> UiText:
    """Подписи интерфейса стенда aid на его языке."""
    return AppLanguage().texts()


@pytest.fixture(scope="session")
def app_config() -> AppConfig:
    built = StandConfigPaths(StandConfigEnv.RUN).load()
    return bind(built, path="app", model=AppConfig)


@pytest.fixture(scope="session", autouse=True)
def app_prefix(app_config: AppConfig) -> None:
    """Как bootstrap: ссылки на файлы и потоки строятся с префиксом приложения."""
    AppPrefix.install(app_config.server.url_prefix)


@pytest.fixture
def files_dir(tmp_path: Path) -> Path:
    return tmp_path / "uploads"


@pytest.fixture
def storage(files_dir: Path) -> LocalStorageClient:
    """Хранилище вложений теста на диске: у приложения его держит сервис
    boba-mcp, тестам слоя данных достаточно каталога."""
    config = LocalStorageConfig(
        files_dir=str(files_dir),
    )

    return LocalStorageClient(config)


@pytest.fixture
def data_bus() -> MemoryMessageBus:
    """Шина слоя данных в тестах: изменения тредов уходят в область пользователя."""
    return memory_message_bus("test-aid")


@pytest.fixture
async def layer(  # noqa: PLR0913 — тестовая фикстура собирает слой с его зависимостями
    app_config: AppConfig,
    pool: AsyncPostgresPool,
    test_postgres: PostgresConfig,
    storage: LocalStorageClient,
    data_bus: MemoryMessageBus,
    runtime_stand: StandRefs,
) -> PostgresDataLayer:
    schema = app_config.db_schema
    async with pool.connection() as conn:
        await conn.execute(
            sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema))
        )
    await StandMigrations().application(test_postgres, schema, "boba.aid")

    await RoleSeed(UsersTable(pool, schema)).add(
        ("read", "wrt", "dev", "analyst", "ADM", "DEV", "GST")
    )
    data_layer = PostgresDataLayer(
        users=UsersTable(pool, schema),
        threads=ThreadsTable(pool, schema),
        feedbacks=FeedbacksTable(pool, schema),
        storage=storage,
        bus=data_bus,
    )
    return data_layer


@pytest.fixture
def auth_token(app_config: AppConfig) -> str:
    secret = app_config.session.auth_secret
    os.environ["CHAINLIT_AUTH_SECRET"] = secret
    return StandTokens.issue(StandTokens.session(AUTH_USER))


@pytest.fixture(autouse=True)
async def chainlit_context(auth_token: str) -> AsyncIterator[None]:
    """Сессия chainlit теста; после него — пустая.

    Async-тесты живут в одном контексте раннера anyio, и поставленная сессия
    пережила бы тест: следующий должен видеть «сессии нет», пока не поставит
    свою.
    """
    from chainlit.context import init_http_context

    init_http_context(user=ChainlitUser(identifier=AUTH_USER), auth_token=auth_token)
    yield
    init_http_context()


@pytest.fixture
async def seeded(
    layer: PostgresDataLayer,
    pool: AsyncPostgresPool,
    app_config: AppConfig,
) -> Seed:
    identity = UserIdentity.model_validate("corporate:user-1")
    await UsersTable(pool, app_config.db_schema).upsert(identity)
    user = await layer.create_user(StandTokens.user(identity.identifier))
    if user is None:
        raise AssertionError("user is not None")

    thread_id = str(uuid4())
    await layer.update_thread(
        thread_id,
        name="thread-1",
        user_id=user.id,
        metadata={"topic": "x"},
        tags=["a"],
    )

    answer_step_id = ChatView.derive_id(thread_id, "m1", StepRole.ANSWER)
    if answer_step_id is None:
        raise AssertionError("answer_step_id is not None")

    return Seed(
        layer=layer,
        user=user,
        thread_id=thread_id,
        answer_step_id=answer_step_id,
    )


class StandTokens(TokenReader):
    """JWT стенда: тот же versioned session contract, что использует AID.

    Секрет читается в момент обращения: фикстуры ставят CHAINLIT_AUTH_SECRET
    позже импорта модуля, а тесты без фикстуры входа живут на своём секрете.
    """

    TTL_SEC: ClassVar[int] = 3600
    FALLBACK_SECRET: ClassVar[str] = "aid-stand-secret"
    GENERATION: ClassVar[str] = "stand-generation"
    ISSUER: ClassVar[str] = "https://aid.stand.invalid"
    CREATED_AT: ClassVar[datetime] = datetime(2026, 1, 1, tzinfo=UTC)

    @classmethod
    def secret(cls) -> str:
        from chainlit.auth.jwt import get_jwt_secret

        secret = get_jwt_secret()
        if secret:
            return secret

        return cls.FALLBACK_SECRET

    @classmethod
    def tokens(cls) -> JwtTokens:
        return JwtTokens(
            cls.secret(),
            SessionLifetime(ttl_sec=cls.TTL_SEC, max_sec=cls.TTL_SEC * 24),
            cls.GENERATION,
            issuer=cls.ISSUER,
            audience=cls.ISSUER,
        )

    @classmethod
    def session(  # noqa: PLR0913 — фабрика явных claims для auth-тестов
        cls,
        identifier: str,
        user_id: UUID | None = None,
        *,
        roles: Collection[str] = (),
        profiles: Collection[str] = ("general",),
        principal: str = "",
        sealed_ticket: str = "",
    ) -> AuthenticatedSession:
        identity = UserIdentity.model_validate(identifier)
        resolved_id = user_id or uuid5(
            NAMESPACE_URL, f"boba-aid-stand:{identity.identifier}"
        )
        credential = (
            DelegatedTicket(principal=principal, sealed=sealed_ticket)
            if principal and sealed_ticket
            else NoUserCredential(code=CredentialAbsence.NOT_DELEGATED)
        )
        renewal = (
            RenewalKind.KERBEROS
            if isinstance(credential, DelegatedTicket)
            else RenewalKind.JWT
        )
        return AuthenticatedSession(
            principal=AuthenticatedPrincipal(
                id=resolved_id,
                identity=identity,
                access=AccessGrant(
                    roles=frozenset(roles), profiles=frozenset(profiles)
                ),
            ),
            credential=credential,
            renewal=renewal,
        )

    @classmethod
    def issue(cls, session: AuthenticatedSession) -> str:
        return cls.tokens().issue(session, int(time.time()))

    @classmethod
    def session_from_metadata(
        cls,
        identifier: str,
        user_id: UUID,
        metadata: Mapping[str, Any],
        *,
        profiles: Collection[str] = ("general",),
    ) -> AuthenticatedSession:
        """Mint a signed test session from explicit inputs, never from a user row."""
        roles = metadata.get(AuthTestClaim.ROLES, ())
        if not isinstance(roles, (list, tuple, set, frozenset)):
            roles = ()
        declared_profiles = metadata.get(AuthTestClaim.PROFILES, profiles)
        if not isinstance(declared_profiles, (list, tuple, set, frozenset)):
            declared_profiles = profiles
        principal = metadata.get(AuthTestClaim.PRINCIPAL, "")
        sealed_ticket = metadata.get(AuthTestClaim.TICKET, "")
        return cls.session(
            identifier,
            user_id,
            roles=roles,
            profiles=declared_profiles,
            principal=principal if isinstance(principal, str) else "",
            sealed_ticket=sealed_ticket if isinstance(sealed_ticket, str) else "",
        )

    @classmethod
    def issue_for(cls, identifier: str, user_id: UUID) -> str:
        """Выпустить JWT для тестовой Chainlit-сессии с заданной строкой users."""
        return cls.issue(cls.session(identifier, user_id))

    @classmethod
    def user(
        cls,
        identifier: str,
        metadata: dict[str, Any] | None = None,
        user_id: str | None = None,
        created_at: str = "2026-01-01T00:00:00+00:00",
    ) -> ChainlitUser:
        """Chainlit-проекция без полномочий: они берутся только из подписанного JWT."""
        identity = UserIdentity.model_validate(identifier)
        resolved_user_id = user_id
        if resolved_user_id is None:
            resolved_user_id = str(
                uuid5(NAMESPACE_URL, f"boba-aid-stand:{identity.identifier}")
            )

        return ChainlitUser(
            identifier=identity.identifier,
            display_name=identity.login,
            metadata=dict(metadata or {}),
        )

    def read(self, token: str) -> SessionClaims:
        return self.tokens().read(token)

    def read_stale(self, token: str, grace_sec: int) -> SessionClaims:
        return self.tokens().read_stale(token, grace_sec)


class SessionStub:
    """Сессия в объёме, который читает ChainlitSession: пользователь и тред.

    Тесты подставляют её вместо живой сессии chainlit, чтобы проверять
    код, которому нужны только user_id и thread_id.
    """

    def __init__(
        self,
        user_id: str | None,
        thread_id: str | None,
        chat_profile: str | None = None,
        identifier: str | None = None,
    ) -> None:
        self.id = "session-stub"
        self.thread_id = thread_id
        self.chat_profile = chat_profile
        self.user = None
        self.token = ""
        if user_id is None and identifier is None:
            return

        name = identifier
        if name is None:
            name = f"user-{user_id}"

        if ":" not in name:
            name = f"corporate:{name}"

        self.user = PersistedUser(
            id=user_id or str(UUID(int=0)),
            identifier=name,
            createdAt="2026-01-01T00:00:00Z",
            metadata={},
        )
        self.token = StandTokens.issue(
            StandTokens.session(
                name,
                UUID(self.user.id),
                profiles={chat_profile or "general"},
            )
        )


class ChatSessionStand:
    """Сессия chainlit теста и контекст вызова, который из неё собрал бы ход.

    Создаётся фикстурой chat_session из стенда контекста вызова теста.
    use() подменяет сессию текущего вызова на подставную; sign_in() ставит
    настоящую сессию chainlit пользователя из таблицы users с JWT его
    входа; enter() ставит контекст вызова из текущей сессии.
    """

    def __init__(self, calls: CallStand, monkeypatch: pytest.MonkeyPatch) -> None:
        self._calls = calls
        self._monkeypatch = monkeypatch

    def use(
        self,
        *,
        user_id: str | None = None,
        thread_id: str | None = None,
        chat_profile: str | None = None,
        identifier: str | None = None,
    ) -> ChainlitSession:
        """Подменяет сессию текущего вызова на подставную; отдаёт её обёртку.

        Полная личность — пользователь и тред — даёт и контекст вызова, как
        его собрал бы ход чата; без неё контекста нет, и инструменты
        отказывают.
        """
        profile = chat_profile
        if profile is None:
            profile = StandIdentity.PROFILE

        stub = SessionStub(user_id, thread_id, profile, identifier)
        session = ChainlitSession(stub, StandTokens())
        # подменяется источник, а не отдельные функции: так стенд попадает во
        # все пути — и в DI-провайдер, и в ref мест вне графа
        self._monkeypatch.setattr(ChainlitSessions, "current", lambda self: session)

        if user_id is not None and thread_id is not None:
            self._calls.use(self._context(session, profile))

        return session

    def enter(self, profile: str = StandIdentity.PROFILE) -> CallContext:
        """Контекст вызова из текущей сессии chainlit — как его собирает on_message.

        Сессии нужны тред, сохранённый пользователь и профиль: тест готовит
        их через init_http_context(user=..., thread_id=...) и chat_profile
        либо зовёт sign_in().
        """
        context = self._context(current_session(), profile)

        return self._calls.use(context)

    @staticmethod
    def _context(session: ChainlitSession, profile: str) -> CallContext:
        """Контекст хода из сессии: пользователь строки users, тред сессии и
        ссылка на делегированный билет из JWT её входа."""
        thread_id = session.thread_id
        if not thread_id:
            raise AssertionError(f"stand session {session.id!r} has no thread")

        user_id = session.user_id
        if not user_id:
            raise AssertionError(f"stand session {session.id!r} has no users row")

        principal = session.authenticated_user()
        subject = Subject.of_user(
            UUID(user_id), principal.identity, principal.roles, profile
        )
        credential = session.signed_in().credential

        return CallContext(
            subject=subject,
            scope=Scope.chat(thread_id),
            run_id=thread_id,
            initiator=ChatInitiator(thread_id=thread_id, turn_id=StandIdentity.TURN),
            credential=credential,
            cancellation=RunCancellation(),
        )

    def sign_in(
        self,
        user: PersistedUser,
        login_metadata: Mapping[str, Any],
        thread_id: str,
        profile: str,
    ) -> str:
        """Сессия пользователя с JWT данного входа и контекст вызова из неё;
        итог — сам токен."""
        from chainlit.context import init_http_context

        token = StandTokens.issue(
            StandTokens.session_from_metadata(
                user.identifier, UUID(user.id), login_metadata, profiles={profile}
            )
        )
        session_user = PersistedUser(
            id=user.id,
            identifier=user.identifier,
            createdAt=user.createdAt,
            metadata={},
        )
        context = init_http_context(
            user=session_user, auth_token=token, thread_id=thread_id
        )
        context.session.chat_profile = profile
        self.enter(profile)

        return token


@pytest.fixture
def chat_session(
    call_stand: CallStand, monkeypatch: pytest.MonkeyPatch
) -> ChatSessionStand:
    """Сессия chainlit теста поверх его стенда контекста вызова."""
    return ChatSessionStand(call_stand, monkeypatch)


@pytest.fixture
def catalog(store: ConnectionStore, call_stand: CallStand) -> Any:
    """connection_list чата над теми же таблицами, что и инструменты: тело
    исполняется в процессе и читает хранилище соединений."""

    return ConnectionTools(store, call_stand.contexts).build()[0]


@pytest.fixture(autouse=True)
def di_root(
    app_config: AppConfig, runtime_stand: StandRefs, storage: LocalStorageClient
) -> Iterator[None]:
    """Корневой контейнер с источником сессий, как его собирает приложение.

    Без него ref-функции падают: отсутствие контейнера — ошибка сборки, а
    не режим работы.
    """
    from boba.aid.agent.chat import ThreadAgentsIndex
    from boba.aid.infra import services as runtime
    from boba.aid.infra.di import Container
    from boba.aid.infra.providers import (
        feed_text,
        mcp_servers,
        remote_journals,
        sandbox_links,
        session_source,
        storage_provider,
        thread_agents_index,
    )

    previous = Container.root
    root = Container(level="app")
    sessions = ChainlitSessions(StandTokens())
    ChainlitSessions.install(sessions)
    root.provide(session_source, sessions)
    root.provide(runtime.application_config, app_config)
    root.provide(runtime.live_locks, MemoryLiveLocks("test-aid", 20))
    root.provide(runtime.message_bus, memory_message_bus("test-aid"))
    root.provide(runtime.payload_store, MemoryPayloadStore())
    root.provide(runtime.call_contexts, runtime_stand.contexts)
    root.provide(runtime.runs, runtime_stand.runs)
    root.provide(remote_journals, RemoteJournals())
    root.provide(sandbox_links, sandbox_links(app_config))
    root.provide(mcp_servers, McpServers(FixedServerConfigs(servers={})))
    root.provide(storage_provider, storage)
    root.provide(thread_agents_index, ThreadAgentsIndex())
    root.provide(feed_text, UiText(AppLanguage.ASSETS, UiText.DEFAULT_LANGUAGE))
    Container.set_root(root)
    try:
        yield
    finally:
        Container.set_root(previous)


class SsoStand:
    """Билеты SSO-входа для стендов: ccache стенда под секретом приложения."""

    @staticmethod
    def tickets(krb5_config: str) -> SsoTickets:
        from chainlit.auth.jwt import get_jwt_secret

        secret = get_jwt_secret()
        if not secret:
            msg = (
                "the stand needs a jwt secret to seal sso tickets: "
                "CHAINLIT_AUTH_SECRET is not set"
            )
            raise RuntimeError(msg)

        return SsoTickets(sealer=TicketSealer(secret), krb5_config=krb5_config)

    @staticmethod
    def sealed(
        tickets: SsoTickets,
        principal: str,
        ccache: str,
        mode: DelegationMode,
        expires_in: int,
    ) -> str:
        data = Path(ccache.removeprefix("FILE:")).read_bytes()
        ticket = SignInTicket(
            principal=principal,
            mode=mode,
            ccache=data,
            expires_at=int(time.time()) + expires_in,
        )

        return tickets.sealer.seal(ticket)


async def put_bytes(storage: StorageClient, object_key: str, data: bytes | str) -> None:
    """Кладёт в хранилище готовые байты теста: запись у хранилища только потоком."""
    await StorageSeed().put(storage, object_key, data)


class FakeTransport(SignalTransport):
    """Транспорт сигналов слежения канваса в тестах: копит payload'ы сигналов
    вместо сокетов; dead имитирует тред без живых вкладок."""

    SIGNAL_SEC: ClassVar[float] = 5.0
    POLL_SEC: ClassVar[float] = 0.01

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []
        self.dead = False

    def alive(self, scope_id: str) -> bool:
        return not self.dead

    async def send(self, scope_id: str, signal: CanvasSignal) -> None:
        self.sent.append(signal.payload())

    async def signaled(self, size: int) -> dict[str, Any]:
        """Сигнал о версии файла размером size. Запись файла проходит через
        промежуточные версии (файл обрезан, тело ещё пишется), и сторож вправе
        сигналить о каждой — панель перечитывает файл и сходится к итогу,
        поэтому ждётся сигнал итоговой версии, а не первый."""
        deadline = time.monotonic() + self.SIGNAL_SEC

        while time.monotonic() < deadline:
            for payload in self.sent:
                if payload["size"] == size:
                    return payload

            await asyncio.sleep(self.POLL_SEC)

        msg = (
            f"canvas watch: expected a signal of the {size}-byte version within "
            f"{self.SIGNAL_SEC:.0f}s, got {self.sent}"
        )
        raise AssertionError(msg)


async def get_bytes(storage: StorageClient, object_key: str) -> bytes:
    """Читает файл хранилища целиком: чтение у хранилища только потоком."""
    window = ReadWindow(offset=0, length=None)
    parts: list[bytes] = []
    async with await storage.open_stream(object_key, window) as opened:
        async for chunk in opened.chunks:
            parts.append(chunk)

    return b"".join(parts)


class RecordingSink(ChatSink):
    """Приёмник ленты стенда: копит шаги как StepDict, тесты сверяют по ним
    отрисованное ChatView."""

    def __init__(self) -> None:
        self._steps: dict[str, StepDict] = {}

    async def put(self, step: Step) -> None:
        self._steps[step.id] = step.to_dict()

    async def drop(self, step: Step) -> None:
        self._steps.pop(step.id, None)

    def rebuilt(self) -> None:
        self._steps.clear()

    @property
    def steps(self) -> list[StepDict]:
        return list(self._steps.values())
