"""Общие фикстуры для тестов PostgresDataLayer."""

import os
import socket
import subprocess
import sys
import time
from collections.abc import (
    AsyncIterator,
    Iterator,
    Mapping,
)
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

import pytest
from chainlit.step import StepDict
from chainlit.user import PersistedUser
from chainlit.user import User as ChainlitUser
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from psycopg import sql

from boba.auth import JwtTokens
from boba.canvas.journal import StreamSlice
from boba.canvas.keys import WorkspaceMount
from boba.chainlit.agent.bridge import ChatModelBridge
from boba.chainlit.canvas.remote import RemoteJournals, RemoteStreams
from boba.chainlit.chat.feed import TurnFeed
from boba.chainlit.chat.history import ThreadMessages, TranscriptFeed
from boba.chainlit.data.data_layer import HeldContent, PostgresDataLayer
from boba.chainlit.domain.keys import AppPrefix, AttachmentLinks
from boba.chainlit.infra.config import AppConfig
from boba.chainlit.infra.session import (
    ChainlitSession,
    ChainlitSessions,
    current_session,
)
from boba.chainlit.rendering.chat_view import (
    ChatSink,
    ChatView,
    LiveSink,
    RecordingSink,
    StepRole,
)
from boba.chainlit.rendering.renderer import ChatRenderer, NoSurface
from boba.config import bind
from boba.connection_broker.store import ConnectionStore
from boba.connection_broker.tools import ConnectionTools
from boba.db.postgres import AsyncPostgresPool
from boba.identity.context import (
    CallContext,
    CallContexts,
    Scope,
)
from boba.identity.errors import RefusalError
from boba.identity.locks import MemoryLiveLocks
from boba.identity.run import ElementTarget, RunPort, RunRefusal, Runs
from boba.identity.session import Login, UserMetadataField
from boba.identity.signin import SignedIn, SignInMetadata
from boba.identity.token import SessionClaims, TokenReader
from boba.kerberos import DelegationMode, SignInTicket
from boba.krb.seal import SsoTickets, TicketSealer
from boba.llm.providers import ChatModelConfig, LlmProviders, LlmProviderTypes
from boba.mcp_client.client import (
    DroppedSignals,
    JournalAddress,
    McpCaller,
    McpServers,
    McpServersConfig,
    NamedBlocks,
)
from boba.messaging import LockToken, MemoryMessageBus, MemoryPayloadStore
from boba.runtime.config import AppLayers
from boba.runtime.elements import ChatTables
from boba.runtime.storage import LocalStorageClient, StorageClient
from boba.stand.refs import StandRefs
from boba.stand.signin import SignInStand
from boba.stand_core.context import CallStand, StandIdentity
from boba.stand_core.fakes import FakeSecret as FakeSecret
from boba.stand_core.fakes import FakeUrl as FakeUrl
from boba.toolkit.channels import JournalChannel
from boba.toolkit.wire import JournalSignal
from boba.toolrun.streams import CallJournals

AUTH_USER = "test-user"


class FakeThreadMessages(ThreadMessages):
    """Источник истории для тестов: сообщения задаются на тред вручную."""

    def __init__(self) -> None:
        self.by_thread: dict[str, list[BaseMessage]] = {}

    async def load(self, thread_id: str) -> list[BaseMessage]:
        return self.by_thread.get(thread_id, [])


class ServiceProcess:
    """Стенд сервиса boba-mcp отдельным процессом; вход — proxy."""

    STAND: ClassVar[Path] = (
        Path(__file__).resolve().parents[4]
        / "packages/apps/boba-mcp/tests/service_stand.py"
    )
    SECRET: ClassVar[str] = "stand-proxy-secret"

    TOKEN: ClassVar[str] = "dev-token"
    """Готовый токен стенда: пользователь alice с ролью dev."""

    OWNER: ClassVar[str] = str(uuid5(NAMESPACE_URL, "boba-mcp:alice"))
    """Под этим id сервис держит журналы и файлы пользователя токена TOKEN."""

    def __init__(self, workdir: Path) -> None:
        self.journal_dir = workdir / "journal"
        """Каталог журналов вызовов стенда сервиса."""
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            self.port = int(probe.getsockname()[1])

        self._process = subprocess.Popen(
            [sys.executable, str(self.STAND), str(self.port), str(workdir)]
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
                "secret": self.SECRET,
                "sign_in": {**location, "path": "/auth/proxy"},
                "headers": {
                    "user": "X-Remote-User",
                    "timestamp": "X-Boba-Timestamp",
                    "signature": "X-Boba-Signature",
                    "roles": "X-Remote-Roles",
                },
            },
        }
        server = {
            "endpoint": endpoint,
            "connect_timeout_sec": 30.0,
            "call_timeout_sec": 60.0,
        }

        return McpServersConfig.model_validate({"servers": {"boba": server}})

    def token_servers(self, name: str) -> McpServersConfig:
        """Секция [mcp.servers] с одним сервером name: вход готовым токеном."""
        server = {
            "endpoint": {
                "transport": "streamable-http",
                "scheme": "http",
                "host": "127.0.0.1",
                "port": self.port,
                "path": "/mcp/service",
                "auth": {"auth": "bearer", "token": self.TOKEN},
            },
            "connect_timeout_sec": 30.0,
            "call_timeout_sec": 60.0,
        }

        return McpServersConfig.model_validate({"servers": {name: server}})

    def stop(self) -> None:
        self._process.terminate()
        self._process.wait(timeout=10)


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
        return McpServers(self._config, NamedBlocks(), DroppedSignals(), self._contexts)

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
        message = ToolMessage(content="", tool_call_id=call_id)
        JournalAddress(server=self.SERVER, run=thread_id).stamp(message)
        self.history.by_thread.setdefault(thread_id, []).append(message)

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
def storage(app_config: AppConfig, files_dir: Path) -> LocalStorageClient:
    config = app_config.storage.model_copy(update={"files_dir": str(files_dir)})
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
        links=AttachmentLinks(app_config.storage.public_prefix),
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


class FakeTurn(RunPort):
    """Ход под тест: реестру достаточно порта, который адресует элемент вызова."""

    ANSWER_STEP: ClassVar[str] = "answer-step"

    def __init__(self) -> None:
        self.shown: list[tuple[str, Mapping[str, Any]]] = []

    async def show_element(self, tool_call_id: str, element: Mapping[str, Any]) -> None:
        self.shown.append((tool_call_id, dict(element)))

    def element_target(self, tool_call_id: str) -> ElementTarget:
        if not tool_call_id:
            msg = f"stand element target needs a tool call id, got {tool_call_id!r}"
            raise RefusalError(RunRefusal.NO_TOOL_CALL, msg)

        return ElementTarget(
            for_id=self.ANSWER_STEP, element_id=f"element-{tool_call_id}"
        )


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


class RecordedTurn:
    """Стенд хода: шина в памяти, рендерер над ChatView и производитель хода.

    Сообщения производителя доходят до ленты синхронно внутри publish, поэтому
    после await любого метода feed лента (sink) уже обновлена.
    """

    def __init__(
        self,
        thread_id: str,
        turn_id: str,
        sink: ChatSink,
        user_name: str = "tester",
    ) -> None:
        self.bus = MemoryMessageBus("test-chainlit")
        self.payloads = MemoryPayloadStore()
        self.sink = sink
        journals = CallJournals(None, Runs(CallContexts()))
        self.view = ChatView(thread_id, sink, journals, user_name=user_name)
        self.renderer = ChatRenderer(thread_id, self.view, self.payloads, NoSurface())
        self.leave = self.bus.subscribe(Scope.chat(thread_id), self.renderer.apply)
        self.feed = TurnFeed(
            self.bus, self.payloads, Scope.chat(thread_id), turn_id, LockToken.local()
        )
        self.renderer.begin_turn(turn_id)

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
    await storage.upload_stream(object_key, HeldContent(data).chunks())
