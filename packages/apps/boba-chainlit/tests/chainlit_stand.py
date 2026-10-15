"""Общие фикстуры тестов chainlit: слой данных, сессии, стенды сервиса инструментов."""

import asyncio
import os
import shutil
import socket
import subprocess
import sys
import time
from collections.abc import (
    AsyncIterator,
    Iterator,
    Mapping,
    Sequence,
)
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, ClassVar
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

import mcp_types as mt
import pytest
from chainlit.user import PersistedUser
from chainlit.user import User as ChainlitUser
from omegaconf import DictConfig, OmegaConf
from psycopg import sql

from boba.auth import JwtTokens
from boba.canvas.journal import StreamSlice
from boba.canvas.keys import WorkspaceMount
from boba.chainlit.agent.config import ChatMcpConfig
from boba.chainlit.agent.relay import McpRelay
from boba.chainlit.agent.server import ChatCallers, HookSealing, KeyHolder, ProcessKeys
from boba.chainlit.canvas.remote import RemoteJournals, RemoteStreams
from boba.chainlit.data.data_layer import PostgresDataLayer
from boba.chainlit.domain.keys import AppPrefix, AttachmentLinks
from boba.chainlit.infra.config import AppConfig
from boba.chainlit.infra.session import (
    ChainlitSession,
    ChainlitSessions,
    OwnerSessions,
    current_session,
)
from boba.chainlit.rendering.acp_view import ToolMedia
from boba.chainlit.rendering.chat_view import ChatView, StepRole
from boba.config import bind
from boba.connection_broker.store import ConnectionStore
from boba.connection_broker.tickets import CredentialsRef
from boba.connection_broker.tools import ConnectionTools
from boba.connection_broker.user_connections import ArmedConnections, StoreRef
from boba.db.postgres import AsyncPostgresPool
from boba.identity.api import StoredUser, UserRows
from boba.identity.context import (
    CallContext,
    CallContexts,
)
from boba.identity.locks import MemoryLiveLocks
from boba.identity.session import Login, SignInProvider, UserMetadataField
from boba.identity.signin import SignedIn, SignInMetadata
from boba.identity.token import SessionClaims, TokenReader
from boba.kerberos import DelegationMode, SignInTicket
from boba.krb.seal import SsoTickets, TicketSealer
from boba.mcp_client.client import (
    McpCaller,
    McpServers,
    McpServersConfig,
)
from boba.messaging import MemoryMessageBus, MemoryPayloadStore
from boba.runtime.config import AppLayers, ConfigLocator
from boba.runtime.elements import ChatTables
from boba.runtime.storage import (
    LocalStorageClient,
    LocalStorageConfig,
    StorageClient,
)
from boba.stand.refs import StandRefs
from boba.stand.service_signin import ServiceSignIn
from boba.stand.storage import StorageSeed
from boba.stand.ui.stand import (
    REPO_ROOT,
    ServiceStand,
    StandApp,
    StandProcess,
    free_port,
)
from boba.stand.zygote import SandboxCgroup
from boba.stand_core.context import CallStand, StandIdentity
from boba.stand_core.context import FakeTurn as FakeTurn
from boba.stand_core.fakes import FakeSecret as FakeSecret
from boba.stand_core.fakes import FakeUrl as FakeUrl
from boba.toolkit.channels import JournalChannel
from boba.toolkit.dag import (
    DagNode,
    JournalAddress,
)
from boba.toolkit.mcp import McpResults, McpToolNames
from boba.toolkit.result import ToolResultBase
from boba.toolkit.wire import ResultWire, WireResult
from boba.transport.acp.protocol import McpErrorOutcome, McpMessage, McpVersion
from boba.workspace.binaries import TrustedBinaries
from boba.workspace.launcher import MountingConfig, ReadWindow

AUTH_USER = "test-user"


class TabSession:
    """Сессия вкладки в объёме, который читает держатель ключа: объект с
    токеном входа, который обновление сессии заменяет на месте."""

    LOGIN: ClassVar[str] = "ivanov"
    PRINCIPAL: ClassVar[str] = "ivanov@STAND.LOCAL"
    SERVER: ClassVar[str] = "boba_general"

    def __init__(self) -> None:
        self.raw = SimpleNamespace(token="")
        self.live = ChainlitSession(self.raw, StandTokens())

    def sign_in(self, roles: frozenset[str], sealed_ticket: str) -> None:
        """Кладёт в сессию токен входа kerberos с ролями и билетом — так же
        токен заменяет обновление сессии."""
        from chainlit.auth.jwt import create_jwt

        metadata = SignInMetadata(
            provider=SignInProvider.KERBEROS.value,
            roles=roles,
            principal=self.PRINCIPAL,
            sealed_ticket=sealed_ticket,
        )
        self.raw.token = create_jwt(StandTokens.user(self.LOGIN, metadata.render()))

    def password_sign_in(self, roles: frozenset[str]) -> None:
        from chainlit.auth.jwt import create_jwt

        metadata = SignInMetadata(provider=SignInProvider.LOCAL.value, roles=roles)
        self.raw.token = create_jwt(StandTokens.user(self.LOGIN, metadata.render()))

    def holder(self, user_id: UUID, profile: str) -> KeyHolder:
        return KeyHolder(
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

    OWNER: ClassVar[str] = str(uuid5(NAMESPACE_URL, "boba-mcp:alice"))
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

    def servers(self) -> McpServersConfig:
        location = {"scheme": "http", "host": "127.0.0.1", "port": self.port}
        endpoint = {
            "transport": "streamable-http",
            **location,
            "path": "/mcp/service",
            "auth": {
                "auth": "proxy",
                "issuer": {**location, "path": "/"},
                "client_id": "stand-chat",
                "client_secret": "stand-client-secret",
                "secret": self.SECRET,
            },
        }
        server = {
            "endpoint": endpoint,
            "connect_timeout_sec": 30.0,
            "call_timeout_sec": 60.0,
        }

        return McpServersConfig.model_validate({"servers": {"boba": server}})

    def token_servers(self, name: str) -> McpServersConfig:
        """Секция [mcp.servers] с одним сервером name: вход токеном
        пользователя alice с ролью dev."""
        path = "/mcp/service"
        token = self._sign_in.token("alice", "dev", f"{self._public}{path}")
        server = {
            "endpoint": {
                "transport": "streamable-http",
                "scheme": "http",
                "host": "127.0.0.1",
                "port": self.port,
                "path": path,
                "auth": {"auth": "bearer", "token": token},
            },
            "connect_timeout_sec": 30.0,
            "call_timeout_sec": 60.0,
        }

        return McpServersConfig.model_validate({"servers": {name: server}})

    def stop(self) -> None:
        self._process.terminate()
        self._process.wait(timeout=10)


class ToolService:
    """Сервис boba-mcp стенда с настоящими плагинами в песочнице, отдельным
    процессом на модуль тестов.

    Нужен тестам, которые проверяют путь вызова целиком: чат запечатывает
    соединение пользователя, сервис открывает его и исполняет тело
    инструмента в песочнице. Создаётся модульной фикстурой теста из
    рабочего каталога и конфига сервиса; порты к нему собирает ServiceTools.
    """

    ENDPOINT: ClassVar[str] = "general"
    BOOT_TIMEOUT_SEC: ClassVar[float] = 300.0
    ROOTFS: ClassVar[str] = "plugins/boba-tool-shell/rootfs.ext4"

    def __init__(self, workdir: Path, service_raw_config: DictConfig) -> None:
        db_name = OmegaConf.select(service_raw_config, "postgres.dbname")
        if not isinstance(db_name, str):
            msg = (
                "tool service stand: section [postgres] of the service config "
                f"expects dbname as a string, got {db_name!r}"
            )
            raise TypeError(msg)

        self._config = ServiceStand(
            workdir=workdir, app_port=free_port(), db_name=db_name, sandbox=True
        )
        self._process = StandProcess[ServiceStand](
            config=self._config, log_path=workdir / "mcp.log"
        )

    @classmethod
    def missing(cls) -> str:
        """Чего сервису не хватает для песочницы на этом хосте; пустая
        строка — всё на месте."""
        app = StandApp.MCP
        if shutil.which("bwrap") is None:
            return "bwrap is not on PATH (build: make fetch sandbox)"

        rootfs = app.sandbox.under(REPO_ROOT) / cls.ROOTFS
        if not rootfs.exists():
            return f"plugin rootfs {rootfs} is not built (make plugin-rootfs-all)"

        return SandboxCgroup().missing()

    @property
    def server_name(self) -> str:
        return self._config.server_name(self.ENDPOINT)

    def start(self) -> None:
        self._process.start(self.BOOT_TIMEOUT_SEC)

    def stop(self) -> None:
        self._process.stop()

    def servers(self) -> McpServersConfig:
        """Секция [mcp.servers] чата с одним сервером — этим сервисом."""
        servers = {self.server_name: self._config.server(self.ENDPOINT)}

        return McpServersConfig.model_validate({"servers": servers})

    def tail(self) -> str:
        return self._process.tail()


class RelayStand:
    """Ретранслятор mcp/message стенда (McpRelay) в сборке, как у провайдера
    процесса: ключи процессов, вызывающие по ключу и запечатывание соединений
    пользователя из хранилища store с кредами credentials.

    Им собирают ретранслятор тесты ретранслятора, вызовы сервиса
    (ServiceTools) и процессы агента стенда (Agents); серверы профиля каждый
    тест открывает сам. Сервер инструментов чата стенду не объявляется.
    """

    CHAT: ClassVar[ChatMcpConfig] = ChatMcpConfig(
        enable=False,
        name="chat",
        path="/chat/mcp",
        connect_timeout_sec=10.0,
        call_timeout_sec=60.0,
    )
    PUBLIC_URL: ClassVar[str] = "http://127.0.0.1"
    """Адрес чата для ретранслятора: без сервера чата по нему никто не ходит."""
    SEAL_TTL: ClassVar[timedelta] = timedelta(minutes=2)

    def __init__(
        self, store: StoreRef, credentials: CredentialsRef, contexts: CallContexts
    ) -> None:
        self.keys = ProcessKeys()
        self.callers = ChatCallers(self.keys)
        connections = ArmedConnections(store, credentials, contexts)
        self.sealing = HookSealing(self.callers, connections, contexts, self.SEAL_TTL)

    def relay(self, servers: McpServers) -> McpRelay:
        return McpRelay(
            self.keys, self.callers, servers, self.sealing, self.CHAT, self.PUBLIC_URL
        )


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
    """Чат к сервису инструментов стенда тем же путём, что вызов агента:
    ретранслятор mcp/message (McpRelay) запечатывает соединения
    (HookSealing) и зовёт MCP-порт сервиса от имени пользователя текущей
    сессии chainlit.

    Создаётся фикстурой теста из сервиса (ToolService), хранилища соединений
    пользователя, его кредов и контекстов вызова; ретранслятор собирает
    RelayStand. Регистрация сервера выдаётся на вызов и снимается после
    него; ответ сервера оживает моделью результата
    (ResultWire), как в ленте чата, иначе — текстом для модели (McpResults).
    """

    def __init__(
        self,
        service: ToolService,
        store: StoreRef,
        credentials: CredentialsRef,
        contexts: CallContexts,
    ) -> None:
        self._service = service
        self._names = McpToolNames()
        self._servers = McpServers(service.servers())
        self._stand = RelayStand(store, credentials, contexts)
        self._relay = self._stand.relay(self._servers)
        self._wire = ResultWire()
        self._results = McpResults()
        self._heard: asyncio.Queue[McpMessage] = asyncio.Queue()

    async def stop(self) -> None:
        await self._servers.stop()

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
        holder = KeyHolder(
            user_id=UUID(user_id),
            login=session.identifier,
            thread_id=thread_id,
            profile=profile,
            session=session,
            server=server,
        )
        keys = self._stand.keys
        registered = keys.register(holder, [server])[0]
        envelope = {
            mt.PROTOCOL_VERSION_META_KEY: McpVersion.V2026_07_28.value,
            mt.CLIENT_CAPABILITIES_META_KEY: {},
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
            keys.revoke(registered.server_id)

        if isinstance(outcome, McpErrorOutcome):
            msg = f"tool {tool!r} of {server!r} answered an MCP error: {outcome.error}"
            raise AssertionError(msg)

        reply = mt.CallToolResult.model_validate(outcome.result)
        full = self._names.full(server, tool)
        wire = WireResult(
            content=reply.content,
            structured=reply.structured_content,
            is_error=reply.is_error,
        )
        revived = self._wire.revived(wire, DagNode(key=call_id, tool=full), server)
        if revived is not None:
            return revived.artifact

        return self._results.result(full, call_id, reply)


class NoConnectionStore:
    """Ссылка на хранилище соединений стенда, которому соединения не нужны:
    обращение к ней — ошибка теста."""

    def __call__(self) -> ConnectionStore:
        msg = "the stand of this test holds no user connections"
        raise AssertionError(msg)


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
    tmp_path_factory: pytest.TempPathFactory, service_raw_config: DictConfig
) -> Iterator[ToolService]:
    """Сервис инструментов модуля: настоящий boba-mcp с плагинами в песочнице."""
    process = ToolService(tmp_path_factory.mktemp("boba-mcp"), service_raw_config)
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

    async def upsert(self, identifier: Login, meta: Mapping[str, Any]) -> StoredUser:
        raise NotImplementedError


class PerCallStreams(RemoteStreams):
    """RemoteStreams тестов панели: на каждое чтение свой клиент MCP.

    Тесты зовут действия панели отдельными asyncio.run, а подключение
    клиента живёт в цикле событий, где открыто: общий клиент второй вызов
    не пережил бы. Реестр журналов при этом один на стенд.

    Клиент чтения не хранится в объекте: показ и сторож слежения читают
    одновременно, и общий клиент один из них закрыл бы под чтением другого.
    """

    def __init__(self, journals: RemoteJournals, caller: McpCaller) -> None:
        self._config = McpServersConfig(servers={})
        super().__init__(journals, self._client(), caller)

    def serve(self, config: McpServersConfig) -> None:
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


@pytest.fixture(scope="session")
def app_config() -> AppConfig:
    built = AppLayers.compose(ConfigLocator.files())
    return bind(built, path="app", model=AppConfig)


@pytest.fixture(scope="session", autouse=True)
def app_prefix(app_config: AppConfig) -> None:
    """Как bootstrap: ссылки на файлы и потоки строятся с префиксом приложения."""
    AppPrefix.install(app_config.chainlit.url_prefix)


@pytest.fixture
def files_dir(tmp_path: Path) -> Path:
    return tmp_path / "uploads"


@pytest.fixture
def storage(files_dir: Path) -> LocalStorageClient:
    """Хранилище вложений теста на диске: у приложения его держит сервис
    boba-mcp, тестам слоя данных достаточно каталога."""
    config = LocalStorageConfig(
        files_dir=str(files_dir),
        mounting=MountingConfig(
            mount_wait_sec=1.0,
            mount_poll_sec=0.1,
            shutdown_wait_sec=1.0,
            lock_wait_sec=1.0,
            copy_chunk_bytes=65536,
        ),
        mount_dir=str(files_dir),
        binaries=TrustedBinaries(dirs=("/usr/bin", "/bin")),
    )

    return LocalStorageClient(config)


@pytest.fixture
def data_bus() -> MemoryMessageBus:
    """Шина слоя данных в тестах: изменения тредов уходят в область пользователя."""
    return MemoryMessageBus("test-chainlit")


@pytest.fixture
async def layer(
    app_config: AppConfig,
    pool: AsyncPostgresPool,
    storage: LocalStorageClient,
    data_bus: MemoryMessageBus,
    runtime_stand: StandRefs,
) -> PostgresDataLayer:
    schema = app_config.data_layer.db_schema
    async with pool.connection() as conn:
        await conn.execute(
            sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema))
        )
    tables = ChatTables.of(app_config.data_layer.postgres, schema, pool)
    await tables.setup()
    data_layer = PostgresDataLayer(
        users=tables.users,
        threads=tables.threads,
        elements=tables.elements,
        feedbacks=tables.feedbacks,
        storage=storage,
        links=AttachmentLinks(app_config.chainlit.url_prefix),
        sessions=ChainlitSessions(StandTokens()),
        bus=data_bus,
    )
    return data_layer


@pytest.fixture
def auth_token(app_config: AppConfig) -> str:
    secret = app_config.session.auth_secret
    os.environ["CHAINLIT_AUTH_SECRET"] = secret

    from chainlit.auth.jwt import create_jwt

    return create_jwt(StandTokens.user(AUTH_USER))


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
async def seeded(layer: PostgresDataLayer) -> Seed:
    user = await layer.create_user(
        ChainlitUser(identifier="user-1", metadata={"role": "tester"})
    )
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
    """JWT стенда: секрет chainlit из окружения, а без него — секрет самого стенда.

    Секрет читается в момент обращения: фикстуры ставят CHAINLIT_AUTH_SECRET
    позже импорта модуля, а тесты без фикстуры входа живут на своём секрете.
    """

    TTL_SEC: ClassVar[int] = 3600
    FALLBACK_SECRET: ClassVar[str] = "chainlit-stand-secret"
    GENERATION: ClassVar[str] = "stand-generation"

    @classmethod
    def secret(cls) -> str:
        from chainlit.auth.jwt import get_jwt_secret

        secret = get_jwt_secret()
        if secret:
            return secret

        return cls.FALLBACK_SECRET

    @classmethod
    def tokens(cls) -> JwtTokens:
        return JwtTokens(cls.secret(), cls.TTL_SEC, cls.GENERATION)

    @classmethod
    def user(
        cls, identifier: str, metadata: dict[str, Any] | None = None
    ) -> ChainlitUser:
        """cl.User стенда с поколением сессий в metadata, как его помечает вход."""
        marked: dict[str, Any] = {UserMetadataField.GENERATION: cls.GENERATION}
        if metadata is not None:
            marked.update(metadata)

        return ChainlitUser(identifier=identifier, metadata=marked)

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

        self.user = PersistedUser(
            id=user_id or str(UUID(int=0)),
            identifier=name,
            createdAt="2026-01-01T00:00:00Z",
            metadata={},
        )
        # токен входа как у живой сессии: без него ход отказывает
        signed = SignedIn(
            identifier=Login(name), display_name="", sign_in=SignInMetadata()
        )
        self.token = StandTokens.tokens().issue(signed)


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
            self._calls.use(session.call_context(StandIdentity.TURN, profile))

        return session

    def enter(self, profile: str = StandIdentity.PROFILE) -> CallContext:
        """Контекст вызова из текущей сессии chainlit — как его собирает on_message.

        Сессии нужны тред, сохранённый пользователь и профиль: тест готовит
        их через init_http_context(user=..., thread_id=...) и chat_profile
        либо зовёт sign_in().
        """
        context = current_session().call_context(StandIdentity.TURN, profile)

        return self._calls.use(context)

    def sign_in(
        self,
        user: PersistedUser,
        login_metadata: Mapping[str, Any],
        thread_id: str,
        profile: str,
    ) -> str:
        """Сессия пользователя с JWT данного входа и контекст вызова из неё;
        итог — сам токен."""
        from chainlit.auth.jwt import create_jwt
        from chainlit.context import init_http_context

        token = create_jwt(StandTokens.user(user.identifier, dict(login_metadata)))
        context = init_http_context(user=user, auth_token=token, thread_id=thread_id)
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

    def stored() -> ConnectionStore:
        return store

    return ConnectionTools(stored, call_stand.contexts).build()[0]


@pytest.fixture(autouse=True)
def di_root(app_config: AppConfig, runtime_stand: StandRefs) -> Iterator[None]:
    """Корневой контейнер с источником сессий, как его собирает приложение.

    Без него ref-функции падают: отсутствие контейнера — ошибка сборки, а
    не режим работы.
    """
    from boba.chainlit.agent.chat import ThreadAgentsIndex
    from boba.chainlit.infra.providers import (
        remote_journals,
        session_source,
        thread_agents_index,
        tool_media,
    )
    from boba.runtime import providers as runtime
    from boba.runtime.di import Container

    previous = Container.root
    root = Container(level="app")
    sessions = ChainlitSessions(StandTokens())
    ChainlitSessions.install(sessions)
    root.provide(session_source, sessions)
    root.provide(runtime.get_runtime_config, app_config)
    root.provide(runtime.live_locks, MemoryLiveLocks("test-chainlit", 20))
    root.provide(runtime.message_bus, MemoryMessageBus("test-chainlit"))
    root.provide(runtime.payload_store, MemoryPayloadStore())
    root.provide(runtime.call_contexts, runtime_stand.contexts)
    root.provide(runtime.runs, runtime_stand.runs)
    root.provide(runtime.call_journals, runtime_stand.journals)
    root.provide(remote_journals, RemoteJournals())
    root.provide(tool_media, ToolMedia())
    root.provide(thread_agents_index, ThreadAgentsIndex())
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


async def get_bytes(storage: StorageClient, object_key: str) -> bytes:
    """Читает файл хранилища целиком: чтение у хранилища только потоком."""
    window = ReadWindow(offset=0, length=None)
    parts: list[bytes] = []
    async with await storage.open_stream(object_key, window) as opened:
        async for chunk in opened.chunks:
            parts.append(chunk)

    return b"".join(parts)
