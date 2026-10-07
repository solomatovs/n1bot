"""Общие фикстуры для тестов PostgresDataLayer."""

import os
import shutil
import socket
import subprocess
import sys
import time
from collections.abc import (
    AsyncIterator,
    Generator,
    Iterator,
    Mapping,
    Sequence,
)
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any, ClassVar
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

import pytest
from chainlit.step import StepDict
from chainlit.user import PersistedUser
from chainlit.user import User as ChainlitUser
from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    ToolMessage,
)
from omegaconf import DictConfig, OmegaConf
from psycopg import sql

from boba.auth import JwtTokens
from boba.auth.credentials import KerberosCredentialSource, NoRefresh
from boba.canvas.journal import StreamSlice
from boba.canvas.keys import WorkspaceMount
from boba.chainlit.agent.bridge import ChatModelBridge, LangchainMessages
from boba.chainlit.canvas.remote import RemoteJournals, RemoteStreams
from boba.chainlit.chat.dialog import (
    DialogMessage,
    PrefetchStage,
    ThreadMessages,
    TurnHistory,
    TurnRecord,
)
from boba.chainlit.chat.feed import TurnFeed
from boba.chainlit.chat.history import TranscriptFeed
from boba.chainlit.chat.turn import ChatTurn, Question
from boba.chainlit.data.data_layer import PostgresDataLayer
from boba.chainlit.data.remote_storage import FileOwners, RemoteStorageClient
from boba.chainlit.domain.keys import AppPrefix, AttachmentLinks, AttachmentUrl
from boba.chainlit.infra.config import AppConfig
from boba.chainlit.infra.session import (
    ChainlitSession,
    ChainlitSessions,
    OwnerSessions,
    current_session,
)
from boba.chainlit.rendering.chat_view import (
    ChatSink,
    ChatView,
    LiveSink,
    RecordingSink,
    StepRole,
)
from boba.chainlit.rendering.mount import ChatCalls, ChatMount
from boba.chainlit.rendering.renderer import ChatRenderer, NoSurface
from boba.chat.profiles import ChatProfileConfig, ChatProfiles
from boba.config import bind
from boba.connection_broker.sealing import SealingToolServer, SentConnections
from boba.connection_broker.store import ConnectionStore
from boba.connection_broker.tools import ConnectionTools
from boba.connection_broker.user_connections import ArmedConnections
from boba.db.postgres import AsyncPostgresPool
from boba.identity.api import StoredUser, UserRows
from boba.identity.context import (
    CallContext,
    CallContexts,
    Scope,
)
from boba.identity.locks import MemoryLiveLocks, RunLocking
from boba.identity.run import Runs
from boba.identity.session import Login, UserMetadataField
from boba.identity.signin import SignedIn, SignInMetadata
from boba.identity.token import SessionClaims, TokenReader
from boba.kerberos import DelegationMode, SignInTicket
from boba.krb.seal import SsoTickets, TicketSealer
from boba.llm.providers import ChatModelConfig, LlmProviders, LlmProviderTypes
from boba.mcp_client.client import (
    DroppedSignals,
    McpCaller,
    McpServers,
    McpServersConfig,
    NamedBlocks,
)
from boba.messaging import LockToken, MemoryMessageBus, MemoryPayloadStore
from boba.runtime.config import AppLayers
from boba.runtime.elements import ChatTables
from boba.runtime.storage import (
    LocalStorageClient,
    LocalStorageConfig,
    StorageClient,
)
from boba.stand.refs import StandRefs
from boba.stand.service_signin import ServiceSignIn
from boba.stand.signin import SignInStand
from boba.stand.storage import StorageSeed
from boba.stand.ui.stand import (
    REPO_ROOT,
    ServiceStand,
    StandApp,
    StandProcess,
    free_port,
)
from boba.stand_core.context import CallStand, StandIdentity
from boba.stand_core.context import FakeTurn as FakeTurn
from boba.stand_core.fakes import FakeSecret as FakeSecret
from boba.stand_core.fakes import FakeUrl as FakeUrl
from boba.toolkit.channels import JournalChannel
from boba.toolkit.dag import (
    DagNode,
    JournalAddress,
    NodeCalls,
    NodeOutcome,
    NodeOutcomes,
    ToolCard,
    ToolServer,
)
from boba.toolkit.result import MarkdownResult, ToolResultBase
from boba.toolkit.wire import JournalSignal
from boba.toolrun.hosted import DirectCalls
from boba.toolrun.streams import CallJournals
from boba.workspace.binaries import TrustedBinaries
from boba.workspace.launcher import MountingConfig, ReadWindow

AUTH_USER = "test-user"


class FakeThreadMessages(ThreadMessages):
    """Источник истории для тестов: сообщения checkpoint'а задаются на тред
    вручную, а чату отдаются сообщениями диалога — тем же переводом, что и
    чтение checkpoint'ов (LangchainMessages)."""

    def __init__(self) -> None:
        self.by_thread: dict[str, list[BaseMessage]] = {}
        self._messages = LangchainMessages()

    async def load(self, thread_id: str) -> Sequence[DialogMessage]:
        return list(self._messages.dialog(self.by_thread.get(thread_id, [])))


class SilentStage(PrefetchStage):
    """Порт этапа подготовки для прогонов без ленты: этап никому не показан."""

    async def begin(self) -> None:
        return None

    async def searching(self, queries: Sequence[str]) -> None:
        return None

    async def end(self, queries: Sequence[str], elapsed_ms: int) -> None:
        return None


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
    CGROUP_PROCS: ClassVar[str] = "cgroup.procs"

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

        procs = Path(app.cgroup_base) / cls.CGROUP_PROCS
        if not os.access(procs, os.W_OK):
            return f"cgroup {app.cgroup_base} is not delegated to the user"

        return ""

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


class ServiceTool:
    """Инструмент сервиса глазами теста: вызов уходит портом чата от имени
    пользователя текущей сессии, ответ — итог вызова порта.

    Создаётся ServiceTools по имени инструмента; интерфейс run тот же,
    что у инструмента хоста, поэтому вызов разбирает boba.stand.toolsetup.Call.
    """

    def __init__(self, name: str, tools: "ServiceTools") -> None:
        self.name = name
        self._tools = tools

    async def run(self, call_id: str, args: Mapping[str, Any]) -> ToolResultBase:
        node = DagNode.model_validate({"key": call_id, "tool": self.name, "args": args})
        outcome = await self._tools.submit(node)

        return outcome.artifact


class ServiceTools:
    """Порт чата к сервису инструментов стенда: тот же состав, что у хода —
    SealingToolServer над MCP-портом сессии.

    Создаётся фикстурой теста из сервиса (ToolService), хранилища соединений
    и источника кредов теста. Вызывающий — пользователь текущей сессии
    chainlit: его логин и роли уходят сервису входом proxy, его соединения
    запечатываются ключом сервиса.
    """

    SEAL_TTL: ClassVar[timedelta] = timedelta(seconds=120)

    def __init__(
        self,
        service: ToolService,
        connections: ArmedConnections,
        contexts: CallContexts,
        calls: NodeCalls,
    ) -> None:
        """calls — путь вызова узла к телу: DirectCalls либо путь чата
        (ChatCalls), когда тесту нужны шаги вызова и монтирование."""
        self._service = service
        self._connections = connections
        self._calls = calls
        self._servers = McpServers(
            service.servers(),
            NamedBlocks(),
            DroppedSignals(),
            contexts,
            calls,
        )
        self.sent = SentConnections()
        """Что ушло сервису вместо ссылок на соединения в идущих вызовах."""

    async def start(self) -> None:
        await self._servers.start()

    async def stop(self) -> None:
        await self._servers.stop()

    def __getitem__(self, name: str) -> ServiceTool:
        return ServiceTool(name, self)

    async def port(self) -> ToolServer:
        """MCP-порт сервиса для пользователя текущей сессии chainlit."""
        session = current_session()
        caller = McpCaller(login=session.identifier, roles=session.roles)

        return await self.port_of(caller)

    async def port_of(self, caller: McpCaller) -> ToolServer:
        """MCP-порт сервиса для названного пользователя и его ролей."""
        ports = await self._servers.for_session(caller, [self._service.server_name])
        if not ports:
            msg = (
                f"tool service {self._service.server_name!r} did not open for "
                f"{caller.login!r}; service log:\n{self._service.tail()}"
            )
            raise AssertionError(msg)

        return ports[0]

    async def submit(self, call: DagNode) -> NodeOutcome:
        port = SealingToolServer(
            await self.port(),
            self._connections,
            self.sent,
            self.SEAL_TTL,
            self._calls,
        )
        pending = await port.submit([call])

        return await pending[0]

    def storage(self, owner: StoredUser, profile: str) -> RemoteStorageClient:
        """Хранилище чата поверх файлов сервиса: владельца ключа оно находит
        по строке users owner, сервер файлов — по профилю profile."""
        profiles = ChatProfiles(
            {
                profile: ChatProfileConfig.model_construct(
                    mcp=[self._service.server_name], roles=["*"], default=True
                )
            }
        )
        sessions = NoTabs()
        users = OwnerRows(owner)
        owners = FileOwners(lambda: sessions, lambda: users, profiles)

        return RemoteStorageClient(AttachmentUrl.MOUNT, owners, lambda: self._servers)


class NoConnectionStore:
    """Ссылка на хранилище соединений стенда, которому соединения не нужны:
    обращение к ней — ошибка теста."""

    def __call__(self) -> ConnectionStore:
        msg = "the stand of this test holds no user connections"
        raise AssertionError(msg)


@dataclass(frozen=True)
class SessionTools:
    """Инструменты сессии и порт инструментов над ними, как их видит граф хода."""

    tools: list[ToolCard]
    service: ToolServer


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


@pytest.fixture
async def service_tools(
    tool_service: ToolService, call_stand: CallStand
) -> AsyncIterator[ServiceTools]:
    """Порт чата к сервису инструментов для тестов без соединений пользователя."""
    credentials = KerberosCredentialSource(None, NoRefresh())
    connections = ArmedConnections(
        NoConnectionStore(), lambda: credentials, call_stand.contexts
    )
    opened = ServiceTools(tool_service, connections, call_stand.contexts, DirectCalls())
    await opened.start()
    try:
        yield opened
    finally:
        await opened.stop()


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

    async def set_llm_settings(
        self, user_id: UUID, profile: str, values: Mapping[str, Any]
    ) -> None:
        raise NotImplementedError


class PerCallStreams(RemoteStreams):
    """RemoteStreams тестов панели: на каждое чтение свой клиент MCP.

    Тесты зовут действия панели отдельными asyncio.run, а подключение
    клиента живёт в цикле событий, где открыто: общий клиент второй вызов
    не пережил бы. Реестр журналов при этом один на стенд.
    """

    def __init__(
        self, journals: RemoteJournals, contexts: CallContexts, caller: McpCaller
    ) -> None:
        self._contexts = contexts
        self._config = McpServersConfig(servers={})
        super().__init__(journals, self._client(), caller)

    def serve(self, config: McpServersConfig) -> None:
        """Серверы, с которых дальше читаются журналы."""
        self._config = config

    def _client(self) -> McpServers:
        return McpServers(
            self._config,
            NamedBlocks(),
            DroppedSignals(),
            self._contexts,
            DirectCalls(),
        )

    async def slice_at(
        self, thread_id: str, call_id: str, channel: JournalChannel, offset: int
    ) -> StreamSlice | None:
        self._servers = self._client()
        await self._servers.start()
        try:
            return await super().slice_at(thread_id, call_id, channel, offset)
        finally:
            await self._servers.stop()

    async def slice_before(
        self, thread_id: str, call_id: str, channel: JournalChannel, end: int
    ) -> StreamSlice | None:
        self._servers = self._client()
        await self._servers.start()
        try:
            return await super().slice_before(thread_id, call_id, channel, end)
        finally:
            await self._servers.stop()


class RemoteStand:
    """Журналы вызовов MCP-серверов для тестов панели.

    Без сервиса (serve() не звали) серверов нет и любой журнал не найден.
    Тест с сервисом зовёт serve() и называет вызовы, чьи журналы лежат на
    сервере, через recorded() — так адрес журнала хранит история треда.
    """

    SERVER: ClassVar[str] = "boba"

    def __init__(self) -> None:
        contexts = CallContexts()
        self.history = FakeThreadMessages()
        self.journals = RemoteJournals(contexts, self.history)
        caller = McpCaller(login=StandIdentity.LOGIN, roles=frozenset())
        self.streams = PerCallStreams(self.journals, contexts, caller)

    def serve(self, service: ServiceProcess) -> None:
        self.streams.serve(service.token_servers(self.SERVER))

    def recorded(self, thread_id: str, call_id: str) -> None:
        """Вызов треда исполнил сервер стенда: запуск — сам тред."""
        self.history.by_thread.setdefault(thread_id, []).append(
            JournaledCall(self.SERVER, thread_id).message(call_id)
        )

    def live(self, thread_id: str, call_id: str, channel: str, size: int) -> None:
        """Сигнал роста журнала идущего вызова: канал дорос до size байт."""
        signal = JournalSignal(
            run=thread_id,
            node=call_id,
            channel=channel,
            size=size,
            closed=False,
            note="",
        )
        self.journals.take(thread_id, self.SERVER, signal)

    def forget(self) -> None:
        """Сброс между тестами: ни истории, ни живых журналов."""
        self.history.by_thread.clear()
        self.journals = RemoteJournals(CallContexts(), self.history)
        self.streams = PerCallStreams(
            self.journals,
            CallContexts(),
            McpCaller(login=StandIdentity.LOGIN, roles=frozenset()),
        )


@dataclass
class Seed:
    """Базовые данные под тест: слой, пользователь, тред и его история."""

    layer: PostgresDataLayer
    user: PersistedUser
    thread_id: str
    messages: list[BaseMessage]
    answer_step_id: str
    """id шага итогового ответа — он же цель для feedback и вложений."""


def in_process_llm(monkeypatch: pytest.MonkeyPatch, app: Any) -> None:
    """Все httpx-клиенты транспорта проекта ходят в ASGI-приложение fake llm
    прямо в процессе теста: сети нет, SSE идёт через ASGITransport."""
    import httpx

    real_client = httpx.AsyncClient

    def client(**kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = httpx.ASGITransport(app=app)  # pyright: ignore[reportArgumentType]
        return real_client(**kwargs)

    monkeypatch.setattr("boba.transport.http.transport.httpx.AsyncClient", client)


def fake_openai_chat(
    model: str = "fake-model",
    sampling: dict[str, Any] | None = None,
) -> ChatModelBridge:
    """Чат-модель прод-стека на провайдере стенда; сеть подменяет in_process_llm."""
    if sampling is None:
        sampling = {}

    cfg = ChatModelConfig(
        provider=SignInStand.provider(), model=model, sampling=sampling
    )
    providers = LlmProviders(LlmProviderTypes.installed())

    return ChatModelBridge(
        chat_model=providers.chat(cfg), sampling=sampling, model_name=model
    )


@pytest.fixture(autouse=True)
def workspace_mount() -> None:
    """Точку рабочего каталога в приложении ставит загрузчик из профиля."""
    WorkspaceMount.configure("/workspace")


@pytest.fixture(scope="session")
def app_config() -> AppConfig:
    config_path = os.environ.get("BOBA_CONFIG_PATH")
    if not config_path:
        raise RuntimeError(
            "the stand expects the app config path in BOBA_CONFIG_PATH, "
            "the variable is not set (launch.json 'pytest: current file' passes it)"
        )
    built = AppLayers.compose(Path(config_path))
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
def thread_messages() -> FakeThreadMessages:
    return FakeThreadMessages()


@pytest.fixture
def data_bus() -> MemoryMessageBus:
    """Шина слоя данных в тестах: изменения тредов уходят в область пользователя."""
    return MemoryMessageBus("test-chainlit")


@pytest.fixture
async def layer(  # noqa: PLR0913 — фикстуры теста
    app_config: AppConfig,
    pool: AsyncPostgresPool,
    storage: LocalStorageClient,
    thread_messages: FakeThreadMessages,
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
        feed=TranscriptFeed(thread_messages, runtime_stand.journals),
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
async def seeded(
    layer: PostgresDataLayer,
    thread_messages: FakeThreadMessages,
) -> Seed:
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

    messages: list[BaseMessage] = [
        HumanMessage(content="hi", id="m1"),
        AIMessage(content="hello", id="m2"),
    ]
    thread_messages.by_thread[thread_id] = messages

    answer_step_id = ChatView.derive_id(thread_id, "m1", StepRole.ANSWER)
    if answer_step_id is None:
        raise AssertionError("answer_step_id is not None")

    return Seed(
        layer=layer,
        user=user,
        thread_id=thread_id,
        messages=messages,
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
    from boba.chainlit.infra.providers import remote_journals, session_source
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
    root.provide(
        remote_journals,
        RemoteJournals(runtime_stand.contexts, FakeThreadMessages()),
    )
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


class JournaledCall:
    """Сообщение инструмента истории о вызове, который исполнил сервер с
    журналом: итог несёт адрес журнала — сервер и запуск на нём. Создаётся
    стендами тестов панели живого вывода; сообщение собирает тот же перевод,
    что и ход чата (LangchainMessages)."""

    TOOL: ClassVar[str] = "bash"

    def __init__(self, server: str, run: str) -> None:
        self._address = JournalAddress(server=server, run=run)
        self._outcomes = NodeOutcomes()
        self._messages = LangchainMessages()

    def message(self, call_id: str) -> ToolMessage:
        call = DagNode(key=call_id, tool=self.TOOL)
        outcome = self._outcomes.of(call, MarkdownResult(text=""), False)
        addressed = outcome.model_copy(update={"journal": self._address})

        return self._messages.tool_message(addressed)


class StandTurnHistory(TurnHistory):
    """Реализация TurnHistory стенда хода: записи исхода копятся в памяти."""

    def __init__(self) -> None:
        self.records: list[TurnRecord] = []

    async def remember(self, record: TurnRecord) -> None:
        self.records.append(record)


class RecordedTurn:
    """Стенд хода: шина в памяти, рендерер над ChatView, производитель хода,
    сам ход (ChatTurn) и путь вызова узла чата (ChatCalls).

    Сообщения производителя доходят до ленты синхронно внутри publish, поэтому
    после await любого метода feed лента (sink) уже обновлена. Шаги вызовов
    инструментов рисует ход port по событиям порта запуска; calls отдаётся
    исполнителю узлов, и под running() его вызовы видны ленте стенда.
    """

    def __init__(
        self,
        thread_id: str,
        turn_id: str,
        sink: ChatSink,
        user_name: str = "tester",
    ) -> None:
        self._thread_id = thread_id
        self.bus = MemoryMessageBus("test-chainlit")
        self.payloads = MemoryPayloadStore()
        self.sink = sink
        self.contexts = CallContexts()
        self.runs = Runs(self.contexts)
        journals = CallJournals(None, self.runs)
        self.view = ChatView(thread_id, sink, journals, user_name=user_name)
        self.renderer = ChatRenderer(thread_id, self.view, self.payloads, NoSurface())
        self.leave = self.bus.subscribe(Scope.chat(thread_id), self.renderer.apply)
        self.feed = TurnFeed(
            self.bus, self.payloads, Scope.chat(thread_id), turn_id, LockToken.local()
        )
        self.renderer.begin_turn(turn_id)
        self._turn_id = turn_id
        self.history = StandTurnHistory()
        self._ports: list[ChatTurn] = []
        self.sent = SentConnections()
        self.calls = ChatCalls(
            self.contexts, self.runs, self.sent, ChatMount(self.contexts, self.runs)
        )

    @property
    def port(self) -> ChatTurn:
        """Ход стенда — владелец запуска. Создаётся при первом обращении:
        его трасеру нужен контекст chainlit, которого у тестов одной ленты
        нет."""
        if not self._ports:
            self._ports.append(
                ChatTurn(
                    thread_id=self._thread_id,
                    feed=self.feed,
                    history=self.history,
                    question=Question(key=self._turn_id, text="question"),
                    locking=RunLocking(
                        locks=MemoryLiveLocks("test:0", 20), heartbeat_sec=1.0
                    ),
                    contexts=self.contexts,
                    runs=self.runs,
                )
            )

        return self._ports[0]

    @contextmanager
    def running(self) -> Generator[ChatTurn, None, None]:
        """Идущий ход стенда: контекст вызова хода и запуск в реестре, чьим
        владельцем стоит port."""
        context = CallStand().context(self._thread_id)
        with self.runs.open(context, self.port):
            yield self.port

    @classmethod
    def recording(
        cls, thread_id: str, turn_id: str, user_name: str = "tester"
    ) -> "RecordedTurn":
        return cls(thread_id, turn_id, RecordingSink(), user_name)

    @classmethod
    def live(
        cls, thread_id: str, turn_id: str, user_name: str = "tester"
    ) -> "RecordedTurn":
        return cls(thread_id, turn_id, LiveSink(), user_name)

    @property
    def recording_sink(self) -> RecordingSink:
        sink = self.sink
        if not isinstance(sink, RecordingSink):
            msg = (
                "steps are recorded only by RecordingSink, the stand sink is "
                f"{type(sink).__name__}"
            )
            raise TypeError(msg)

        return sink

    @property
    def steps(self) -> list[StepDict]:
        return self.recording_sink.steps


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
