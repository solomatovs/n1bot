"""Общие фикстуры для тестов PostgresDataLayer."""

import os
import secrets
from collections.abc import AsyncIterator, Iterable, Iterator
from contextvars import ContextVar
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import ClassVar
from uuid import uuid4

import pytest
from chainlit.user import PersistedUser
from chainlit.user import User as ChainlitUser
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from omegaconf import DictConfig
from psycopg import sql

from boba.cancellation import RunCancellation
from boba.canvas.keys import WorkspaceMount
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
from boba.chainlit.rendering.chat_view import ChatView, StepRole
from boba.chat.openai import OpenAiConfig
from boba.chat.provider import ChatSampling, OpenAiChatConfig
from boba.db.postgres import AsyncPostgresPool
from boba.identity.context import (
    CallContext,
    ChatInitiator,
    NoUserCredential,
    Scope,
    Subject,
)
from boba.identity.errors import RefusalError
from boba.identity.run import ElementTarget, RunPort, RunRefusal
from boba.llm.bridge import ProviderChatModel
from boba.llm.openai_chat import OpenAiChatProvider
from boba.settings import bind, build_app_config

TEST_DB = "boba_chainlit_test"
AUTH_USER = "test-user"


class FakeSecret(StrEnum):
    """Заглушки секретов для тестов: значение рождается на запуске, не в коде."""

    LDAP_BIND = secrets.token_hex(8)
    AUTH = secrets.token_hex(8)
    DB = secrets.token_hex(8)
    DB_OTHER = secrets.token_hex(8)
    HTTP_BASIC = secrets.token_hex(8)
    HTTP_BEARER = secrets.token_hex(8)


class FakeUrl(StrEnum):
    """Адреса-заглушки: запросы уходят в ASGI-приложение, а не в сеть."""

    BASE = "https://boba"
    WORKSPACE = "https://boba/workspace"
    LOOPBACK_SCHEME = "http"
    LOOPBACK_HOST = "127.0.0.1"

    @classmethod
    def loopback(cls, port: int, path: str = "") -> str:
        """Адрес локального стенда: сервер поднимается тестом, TLS ему негде взять."""
        return f"{cls.LOOPBACK_SCHEME}://{cls.LOOPBACK_HOST}:{port}{path}"


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
    sampling: ChatSampling | None = None,
) -> ProviderChatModel:
    """Чат-модель прод-стека на фейковом httpx-клиенте: SSE идёт через него."""
    cfg = OpenAiChatConfig(
        provider="openai",
        openai=OpenAiConfig(base_url=base_url, api_key="fake-key"),
    )

    if sampling is None:
        sampling = ChatSampling()

    import httpx

    if not isinstance(client, httpx.AsyncClient):
        raise TypeError(f"fake client must be httpx.AsyncClient, got {type(client)}")

    provider = OpenAiChatProvider(cfg, client, model)
    return ProviderChatModel(provider=provider, sampling=sampling, model_name=model)


@pytest.fixture(scope="session")
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(autouse=True)
def workspace_mount() -> None:
    """Точку рабочего каталога в приложении ставит загрузчик из профиля."""
    WorkspaceMount.configure("/workspace")


@pytest.fixture(scope="session", autouse=True)
def kerberos_workspace(tmp_path_factory: pytest.TempPathFactory) -> None:
    """Кэши билетов теста: приложение раскладывает их само, как в бою."""
    from boba.krb import KerberosWorkspace

    krb = Path(__file__).resolve().parents[4] / "compose" / "conf" / "krb"
    cache = tmp_path_factory.mktemp("krb-cache")
    KerberosWorkspace.configure(str(krb / "krb5.conf"), str(cache))


@pytest.fixture(scope="session")
def raw_config() -> DictConfig:
    """Собранный конфиг приложения до привязки к моделям."""
    config_path = os.environ.get("BOBA_CONFIG_PATH")
    if not config_path:
        raise RuntimeError(
            "BOBA_CONFIG_PATH не задан — укажи конфиг приложения "
            "(launch.json 'pytest: текущий файл' его прокидывает)"
        )
    return build_app_config(config_path=Path(config_path))


@pytest.fixture(scope="session")
def app_config() -> AppConfig:
    config_path = os.environ.get("BOBA_CONFIG_PATH")
    if not config_path:
        raise RuntimeError(
            "BOBA_CONFIG_PATH не задан — укажи конфиг приложения "
            "(launch.json 'pytest: текущий файл' его прокидывает)"
        )
    built = build_app_config(config_path=Path(config_path))
    return bind(built, path="app", model=AppConfig)


@pytest.fixture(scope="session")
async def test_database(app_config: AppConfig) -> str:
    """Создаёт тестовую БД через общий пул: свой keytab, а не чужой krb5-кэш."""
    maintenance = AsyncPostgresPool(app_config.data_layer.postgres)
    await maintenance.open()
    try:
        async with maintenance.cursor() as cur:
            await cur.execute(
                """
                select
                    1
                from
                    pg_database
                where
                    datname = %s
                """,
                (TEST_DB,),
            )
            exists = await cur.fetchone()
            if not exists:
                await cur.execute(
                    sql.SQL("create database {}").format(sql.Identifier(TEST_DB))
                )
    finally:
        await maintenance.close()

    return TEST_DB


@pytest.fixture
async def pool(
    app_config: AppConfig, test_database: str
) -> AsyncIterator[AsyncPostgresPool]:
    p = AsyncPostgresPool(
        app_config.data_layer.postgres.model_copy(update={"dbname": test_database}),
        override_options={"search_path": app_config.data_layer.db_schema},
    )
    await p.open()
    try:
        yield p
    finally:
        await p.close()


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
async def layer(
    app_config: AppConfig,
    pool: AsyncPostgresPool,
    storage: LocalStorageClient,
    thread_messages: FakeThreadMessages,
) -> PostgresDataLayer:
    schema = app_config.data_layer.db_schema
    async with pool.connection() as conn:
        await conn.execute(
            sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema))
        )
    data_layer = PostgresDataLayer(
        pool,
        schema=schema,
        storage=storage,
        feed=TranscriptFeed(thread_messages),
        links=AttachmentLinks(app_config.storage.public_prefix),
        sessions=ChainlitSessions(),
    )
    await data_layer.setup()
    return data_layer


@pytest.fixture
def auth_token(app_config: AppConfig) -> str:
    secret = app_config.chainlit.auth_secret
    if not secret:
        raise RuntimeError("chainlit.auth_secret не задан в конфиге")
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


TEST_TURN = "test-turn"
"""Метка хода в контекстах вызова, которые ставят тесты."""

TEST_PROFILE = "test"
"""Профиль контекста вызова, если тест не назвал свой."""


@pytest.fixture(autouse=True)
def call_context_cleared() -> Iterator[None]:
    """Контекст вызова — contextvar: без сброса он утёк бы между sync-тестами."""
    CallContext.reset()
    yield
    CallContext.reset()


def install_context(monkeypatch: pytest.MonkeyPatch, context: CallContext) -> None:
    """Ставит контекст вызова на время теста в любом контексте исполнения.

    Async-тесты живут в одном контексте раннера anyio, и set() на contextvar
    пережил бы тест; подмена самой переменной снимается monkeypatch'ем.
    """
    current: ContextVar[CallContext | None] = ContextVar(
        "boba_call_context", default=context
    )
    monkeypatch.setattr(CallContext, "_CURRENT", current)


def make_context(  # noqa: PLR0913 — личность собирается по частям, как в сессии
    thread_id: str,
    cancellation: RunCancellation | None = None,
    *,
    user_id: int = 7,
    login: str = "tester",
    roles: Iterable[str] = (),
    profile: str = TEST_PROFILE,
) -> CallContext:
    """Контекст хода чата, как его собирает on_message, без сессии chainlit."""
    if cancellation is None:
        cancellation = RunCancellation()

    return CallContext(
        subject=Subject(
            user_id=user_id, login=login, roles=frozenset(roles), profile=profile
        ),
        scope=Scope.chat(thread_id),
        initiator=ChatInitiator(thread_id=thread_id, turn_id=TEST_TURN),
        credential=NoUserCredential(reason="the test context carries no ticket"),
        cancellation=cancellation,
    )


def use_context(  # noqa: PLR0913 — личность собирается по частям, как в сессии
    monkeypatch: pytest.MonkeyPatch,
    *,
    thread_id: str,
    user_id: int = 7,
    login: str = "tester",
    roles: Iterable[str] = (),
    profile: str = TEST_PROFILE,
) -> CallContext:
    """Ставит контекст вызова хода чата на время теста."""
    context = make_context(
        thread_id, user_id=user_id, login=login, roles=roles, profile=profile
    )
    install_context(monkeypatch, context)

    return context


class FakeTurn(RunPort):
    """Ход под тест: реестру достаточно порта, который адресует элемент вызова."""

    ANSWER_STEP: ClassVar[str] = "answer-step"

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


SESSIONS = ChainlitSessions()
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
        if user_id is None and identifier is None:
            return

        name = identifier
        if name is None:
            name = f"user-{user_id}"

        self.user = PersistedUser(
            id=user_id or "0",
            identifier=name,
            createdAt="2026-01-01T00:00:00Z",
            metadata={},
        )


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
    session = ChainlitSession(stub)
    # подменяется источник, а не отдельные функции: так стенд попадает во
    # все пути — и в DI-провайдер, и в ref мест вне графа
    monkeypatch.setattr(ChainlitSessions, "current", lambda self: session)

    if user_id is not None and thread_id is not None:
        install_context(monkeypatch, session.call_context(TEST_TURN, profile))

    return session


@pytest.fixture(autouse=True)
def di_root() -> Iterator[None]:
    """Корневой контейнер с источником сессий, как его собирает приложение.

    Без него ref-функции падают: отсутствие контейнера — ошибка сборки, а
    не режим работы.
    """
    from boba.chainlit.infra.providers import session_source
    from boba.runtime.di import Container

    previous = Container.root
    root = Container(level="app")
    root.provide(session_source, ChainlitSessions())
    Container.set_root(root)
    try:
        yield
    finally:
        Container.set_root(previous)
