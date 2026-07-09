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
from chainlit.step import StepDict
from chainlit.user import PersistedUser
from chainlit.user import User as ChainlitUser
from psycopg import AsyncConnection, sql
from psycopg_pool import AsyncConnectionPool

from boba.agent.tool_config import bind, build_app_config
from boba.chainlit2.chat.data.data_layer import PostgresDataLayer
from boba.chainlit2.chat.data.models import Codec
from boba.chainlit2.chat.data.storage import LocalStorageClient
from boba.chainlit2.infra.config import AppConfig

# отдельная БД под тесты: реальный сервер из конфига, но не боевая база
TEST_DB = "boba_chainlit_test"
# идентификатор аутентифицированного пользователя chainlit-контекста
AUTH_USER = "test-user"


@dataclass
class Seed:
    """Базовые данные под тест: слой + созданные пользователь/тред/шаг."""

    layer: PostgresDataLayer
    user: PersistedUser
    thread_id: str
    step_id: str
    step: StepDict


@pytest.fixture(scope="session")
def anyio_backend() -> str:
    """anyio гоняет async-тесты/фикстуры только на asyncio."""
    return "asyncio"


@pytest.fixture(scope="session")
def app_config() -> AppConfig:
    """Реальный конфиг приложения из BOBA_CONFIG_PATH (как в проде через DI)."""
    config_path = os.environ.get("BOBA_CONFIG_PATH")
    if not config_path:
        raise RuntimeError(
            "BOBA_CONFIG_PATH не задан — укажи конфиг приложения "
            "(launch.json 'Chainlit2: pytest current file' его прокидывает)"
        )
    built = build_app_config(config_path=Path(config_path))
    return bind(built, path="app", model=AppConfig)


@pytest.fixture(scope="session")
def pg_kwargs(app_config: AppConfig) -> dict:
    """psycopg-параметры коннекта из конфига с подменённым dbname на тестовый."""
    kwargs = app_config.postgres.conn_settings(
        {"search_path": app_config.data_layer.schema}
    )
    kwargs["dbname"] = TEST_DB
    return kwargs


@pytest.fixture(scope="session")
def test_database(app_config: AppConfig) -> str:
    """Создаёт тестовую БД на сервере из конфига, если её ещё нет."""
    maintenance = app_config.postgres.conn_settings()  # боевой dbname служебный
    with psycopg.connect(**maintenance, connect_timeout=5) as conn:
        exists = conn.execute(
            "select 1 from pg_database where datname = %s", (TEST_DB,)
        ).fetchone()
        if not exists:
            conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(TEST_DB)))
    return TEST_DB


@pytest.fixture
async def pool(
    app_config: AppConfig, pg_kwargs: dict, test_database: str
) -> AsyncIterator[AsyncConnectionPool]:
    """
    Пул на тестовую БД; конструируется как прод-провайдер (kwargs + pool_settings)
    """
    p = AsyncConnectionPool(
        connection_class=AsyncConnection,
        kwargs=pg_kwargs,
        open=False,
        **app_config.postgres.pool_settings(),
    )
    await p.open()
    try:
        yield p
    finally:
        await p.close()


@pytest.fixture
def files_dir(tmp_path: Path) -> Path:
    """Изолированная папка под вложения (в проектную ./upload не пишем)."""
    return tmp_path / "uploads"


@pytest.fixture
def storage(app_config: AppConfig, files_dir: Path) -> LocalStorageClient:
    """Настоящий дисковый storage-клиент проекта (files_dir — во временную папку)."""
    config = app_config.storage.model_copy(update={"files_dir": str(files_dir)})
    return LocalStorageClient(config)


@pytest.fixture
async def layer(
    app_config: AppConfig, pool: AsyncConnectionPool, storage: LocalStorageClient
) -> PostgresDataLayer:
    """Слой со свежей схемой: дропаем схему и заново гоняем setup() на каждый тест."""
    schema = app_config.data_layer.schema
    async with pool.connection() as conn:
        await conn.execute(
            sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema))
        )
    data_layer = PostgresDataLayer(pool, schema=schema, storage=storage)
    await data_layer.setup()
    return data_layer


@pytest.fixture
def auth_token(app_config: AppConfig) -> str:
    """Настоящий chainlit JWT, подписанный auth-секретом из конфига."""
    secret = app_config.chainlit.auth_secret
    if not secret:
        raise RuntimeError("chainlit.auth_secret не задан в конфиге")
    os.environ["CHAINLIT_AUTH_SECRET"] = secret

    from chainlit.auth.jwt import create_jwt

    return create_jwt(ChainlitUser(identifier=AUTH_USER))


@pytest.fixture(autouse=True)
async def chainlit_context(auth_token: str) -> None:
    """Аутентифицированный HTTP-контекст chainlit (JWT из конфига, нужен event loop).

    Методы записи обёрнуты @queue_until_user_message: с HTTP-сессией (не websocket)
    декоратор выполняет тело сразу, а не кладёт в очередь.
    """
    from chainlit.context import init_http_context

    init_http_context(user=ChainlitUser(identifier=AUTH_USER), auth_token=auth_token)


@pytest.fixture
async def seeded(layer: PostgresDataLayer) -> Seed:
    """Базовые данные: пользователь + тред + один (избранный) шаг."""
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

    step_id = str(uuid4())
    step: StepDict = {
        "id": step_id,
        "threadId": thread_id,
        "type": "assistant_message",
        "name": "assistant",
        "input": "hi",
        "output": "hello",
        "createdAt": Codec.iso(Codec.now()),
        "metadata": {"favorite": True},
    }
    await layer.create_step(step)

    return Seed(
        layer=layer,
        user=user,
        thread_id=thread_id,
        step_id=step_id,
        step=step,
    )
