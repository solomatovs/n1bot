"""Общие фикстуры для тестов PostgresDataLayer."""

import os
import time
from collections.abc import (
    AsyncIterator,
    Iterator,
    Mapping,
)
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar
from uuid import UUID, uuid4

import pytest
from chainlit.step import StepDict
from chainlit.user import PersistedUser
from chainlit.user import User as ChainlitUser
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from psycopg import sql

from boba.auth import JwtTokens
from boba.canvas.keys import WorkspaceMount
from boba.chainlit.chat.feed import TurnFeed
from boba.chainlit.chat.history import ThreadMessages, TranscriptFeed
from boba.chainlit.data.data_layer import PostgresDataLayer
from boba.chainlit.data.storage import LocalStorageClient
from boba.chainlit.domain.keys import AttachmentLinks
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
from boba.chat.openai import OpenAiConfig
from boba.chat.provider import OpenAiChatConfig
from boba.config import bind
from boba.db.postgres import AsyncPostgresPool
from boba.identity.context import (
    CallContext,
    Scope,
)
from boba.identity.errors import RefusalError
from boba.identity.locks import MemoryLiveLocks
from boba.identity.run import ElementTarget, RunPort, RunRefusal
from boba.identity.signin import SignedIn, SignInMetadata
from boba.identity.token import SessionClaims, TokenReader
from boba.kerberos import DelegationMode, SignInTicket
from boba.krb.seal import SsoTickets, TicketSealer
from boba.llm.bridge import ProviderChatModel
from boba.llm.openai_chat import OpenAiChatProvider
from boba.messaging import LockToken, MemoryMessageBus, MemoryPayloadStore
from boba.runtime.config import AppLayers
from boba.runtime.elements import ChatTables
from boba.stand.context import TEST_PROFILE as TEST_PROFILE
from boba.stand.context import TEST_TURN as TEST_TURN
from boba.stand.context import install_context as install_context
from boba.stand.context import make_context as make_context
from boba.stand.context import use_context as use_context
from boba.stand.fakes import FakeSecret as FakeSecret
from boba.stand.fakes import FakeUrl as FakeUrl

AUTH_USER = "test-user"


class FakeThreadMessages(ThreadMessages):
    """Источник истории для тестов: сообщения задаются на тред вручную."""

    def __init__(self) -> None:
        self.by_thread: dict[str, list[BaseMessage]] = {}

    async def load(self, thread_id: str) -> list[BaseMessage]:
        return self.by_thread.get(thread_id, [])


@dataclass
class Seed:
    """Базовые данные под тест: слой, пользователь, тред и его история."""

    layer: PostgresDataLayer
    user: PersistedUser
    thread_id: str
    messages: list[BaseMessage]
    answer_step_id: str
    """id шага итогового ответа — он же цель для feedback и вложений."""


def fake_openai_chat(
    client: object,
    model: str = "fake-model",
    base_url: str = "https://fake-llm/v1",
    sampling: dict[str, Any] | None = None,
) -> ProviderChatModel:
    """Чат-модель прод-стека на фейковом httpx-клиенте: SSE идёт через него."""
    cfg = OpenAiChatConfig(
        kind="openai",
        http=OpenAiConfig(base_url=base_url, api_key="fake-key"),
    )

    if sampling is None:
        sampling = {}

    import httpx

    if not isinstance(client, httpx.AsyncClient):
        raise TypeError(f"fake client must be httpx.AsyncClient, got {type(client)}")

    provider = OpenAiChatProvider(cfg, client, model)
    return ProviderChatModel(provider=provider, sampling=sampling, model_name=model)


@pytest.fixture(autouse=True)
def workspace_mount() -> None:
    """Точку рабочего каталога в приложении ставит загрузчик из профиля."""
    WorkspaceMount.configure("/workspace")


@pytest.fixture(scope="session")
def app_config() -> AppConfig:
    config_path = os.environ.get("BOBA_CONFIG_PATH")
    if not config_path:
        raise RuntimeError(
            "BOBA_CONFIG_PATH не задан — укажи конфиг приложения "
            "(launch.json 'pytest: текущий файл' его прокидывает)"
        )
    built = AppLayers.compose(Path(config_path))
    return bind(built, path="app", model=AppConfig)


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
async def layer(
    app_config: AppConfig,
    pool: AsyncPostgresPool,
    storage: LocalStorageClient,
    thread_messages: FakeThreadMessages,
    data_bus: MemoryMessageBus,
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
        feed=TranscriptFeed(thread_messages),
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

    return create_jwt(ChainlitUser(identifier=AUTH_USER))


@pytest.fixture(autouse=True)
async def chainlit_context(auth_token: str) -> AsyncIterator[None]:
    """Сессия chainlit теста; после него — пустая.

    Async-тесты живут в одном контексте раннера anyio, и поставленная сессия
    пережила бы тест: следующий должен видеть «сессии нет», пока не поставит
    свою.
    """
    from chainlit.context import init_http_context

    CallContext.reset()
    init_http_context(user=ChainlitUser(identifier=AUTH_USER), auth_token=auth_token)
    yield
    CallContext.reset()
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
            raise RefusalError(RunRefusal.NO_TOOL_CALL, "tool call without id")

        return ElementTarget(
            for_id=self.ANSWER_STEP, element_id=f"element-{tool_call_id}"
        )


def enter_context(profile: str = TEST_PROFILE) -> CallContext:
    """Контекст вызова из текущей сессии chainlit — как его собирает on_message.

    Сессии нужны тред, сохранённый пользователь и профиль: тест готовит их
    через init_http_context(user=..., thread_id=...) и chat_profile.
    """
    context = current_session().call_context(TEST_TURN, profile)
    CallContext._CURRENT.set(context)

    return context


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

    @classmethod
    def secret(cls) -> str:
        from chainlit.auth.jwt import get_jwt_secret

        secret = get_jwt_secret()
        if secret:
            return secret

        return cls.FALLBACK_SECRET

    @classmethod
    def tokens(cls) -> JwtTokens:
        return JwtTokens(cls.secret(), cls.TTL_SEC)

    def read(self, token: str) -> SessionClaims:
        return self.tokens().read(token)

    def read_stale(self, token: str, grace_sec: int) -> SessionClaims:
        return self.tokens().read_stale(token, grace_sec)


SESSIONS = ChainlitSessions(StandTokens())
"""Источник сессий для тестов: подмену ставит use_session на класс."""


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
        signed = SignedIn(identifier=name, display_name="", sign_in=SignInMetadata())
        self.token = StandTokens.tokens().issue(signed)


def use_session(
    monkeypatch: pytest.MonkeyPatch,
    *,
    user_id: str | None = None,
    thread_id: str | None = None,
    chat_profile: str | None = None,
    identifier: str | None = None,
) -> ChainlitSession:
    """Подменяет сессию текущего вызова на подставную; отдаёт её обёртку.

    Полная личность — пользователь и тред — даёт и контекст вызова, как
    его собрал бы ход чата; без неё контекста нет, и инструменты отказывают.
    """
    profile = chat_profile
    if profile is None:
        profile = TEST_PROFILE

    stub = SessionStub(user_id, thread_id, profile, identifier)
    session = ChainlitSession(stub, StandTokens())
    # подменяется источник, а не отдельные функции: так стенд попадает во
    # все пути — и в DI-провайдер, и в ref мест вне графа
    monkeypatch.setattr(ChainlitSessions, "current", lambda self: session)

    if user_id is not None and thread_id is not None:
        install_context(monkeypatch, session.call_context(TEST_TURN, profile))

    return session


@pytest.fixture(autouse=True)
def di_root(app_config: AppConfig) -> Iterator[None]:
    """Корневой контейнер с источником сессий, как его собирает приложение.

    Без него ref-функции падают: отсутствие контейнера — ошибка сборки, а
    не режим работы.
    """
    from boba.chainlit.infra.providers import session_source
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
            raise RuntimeError("CHAINLIT_AUTH_SECRET is not set for the stand")

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
        self.view = ChatView(thread_id, sink, user_name=user_name)
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
            msg = "steps are recorded only by RecordingSink"
            raise TypeError(msg)

        return sink

    @property
    def steps(self) -> list[StepDict]:
        return self.recording_sink.steps
