"""
Общие фикстуры для тестов PostgresDataLayer
"""

import os
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from chainlit.user import PersistedUser
from chainlit.user import User as ChainlitUser
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from omegaconf import DictConfig
from psycopg import sql

from boba.chainlit.chat.data.data_layer import PostgresDataLayer
from boba.chainlit.chat.data.storage import LocalStorageClient
from boba.chainlit.infra.config import AppConfig
from boba.chainlit.rendering.chat_view import ChatView
from boba.db.postgres import AsyncPostgresPool
from boba.settings import bind, build_app_config

TEST_DB = "boba_chainlit_test"
AUTH_USER = "test-user"


class FakeThreadMessages:
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


@pytest.fixture(scope="session")
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(scope="session")
def raw_config() -> DictConfig:
    """Собранный конфиг приложения до привязки к моделям."""
    config_path = os.environ.get("BOBA_CONFIG_PATH")
    if not config_path:
        raise RuntimeError(
            "BOBA_CONFIG_PATH не задан — укажи конфиг приложения "
            "(launch.json 'Chainlit2: pytest current file' его прокидывает)"
        )
    return build_app_config(config_path=Path(config_path))


@pytest.fixture(scope="session")
def app_config() -> AppConfig:
    config_path = os.environ.get("BOBA_CONFIG_PATH")
    if not config_path:
        raise RuntimeError(
            "BOBA_CONFIG_PATH не задан — укажи конфиг приложения "
            "(launch.json 'Chainlit2: pytest current file' его прокидывает)"
        )
    built = build_app_config(config_path=Path(config_path))
    return bind(built, path="app", model=AppConfig)


@pytest.fixture(scope="session")
def test_database(app_config: AppConfig) -> str:
    maintenance = app_config.data_layer.postgres.conn_settings()
    with psycopg.connect(**maintenance, connect_timeout=5) as conn:
        exists = conn.execute(
            "select 1 from pg_database where datname = %s", (TEST_DB,)
        ).fetchone()
        if not exists:
            conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(TEST_DB)))
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
        messages=thread_messages,
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
async def chainlit_context(auth_token: str) -> None:
    from chainlit.context import init_http_context

    init_http_context(user=ChainlitUser(identifier=AUTH_USER), auth_token=auth_token)


@pytest.fixture
async def seeded(
    layer: PostgresDataLayer,
    thread_messages: FakeThreadMessages,
) -> Seed:
    user = await layer.create_user(
        ChainlitUser(identifier="user-1", metadata={"role": "tester"})
    )
    assert user is not None

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

    answer_step_id = ChatView.derive_id(thread_id, "m1", "answer")
    assert answer_step_id is not None

    return Seed(
        layer=layer,
        user=user,
        thread_id=thread_id,
        messages=messages,
        answer_step_id=answer_step_id,
    )
