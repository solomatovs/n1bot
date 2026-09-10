"""Пулы postgres процесса chainlit: системные компоненты живут на одном пуле.

Контейнер собирается как в bootstrap, но на тестовой базе, и тест считает пулы
процесса. Свой пул положен только checkpointer'у: AsyncPostgresSaver пишет имена
таблиц без схемы и ходит по search_path. Остальные ставят схему в запрос, и
общего пула им достаточно.
"""

from __future__ import annotations

import copy
from collections.abc import AsyncIterator

import pytest
from chainlit_stand import StandTokens
from omegaconf import DictConfig, OmegaConf

from boba.chainlit.data.data_layer import PostgresDataLayer
from boba.chainlit.infra import providers
from boba.chainlit.infra.config import AppConfig
from boba.chainlit.infra.session import ChainlitSessions
from boba.config import bind
from boba.db.postgres import AsyncPostgresPool
from boba.runtime import providers as runtime
from boba.runtime.config import AppName
from boba.runtime.di import Container

pytestmark = pytest.mark.anyio


@pytest.fixture(autouse=True)
async def no_pools() -> AsyncIterator[None]:
    """Счёт идёт с нуля: пулы процессные и переживают тест, который их открыл."""
    await AsyncPostgresPool.close_all()
    yield
    await AsyncPostgresPool.close_all()


@pytest.fixture
def stand_raw(raw_config: DictConfig, test_database: str) -> DictConfig:
    """Конфиг стенда, у которого общий [postgres] смотрит в тестовую базу."""
    raw = copy.deepcopy(raw_config)
    OmegaConf.update(raw, "postgres.dbname", test_database)

    return raw


@pytest.fixture
def stand_config(stand_raw: DictConfig) -> AppConfig:
    return bind(stand_raw, path="app", model=AppConfig)


@pytest.fixture
def app_container(
    stand_raw: DictConfig,
    stand_config: AppConfig,
    storage: object,
) -> Container:
    """Провайдеры chainlit, которым нужен postgres; тяжёлое подставлено готовым."""
    container = Container(level="app")
    container.provide(providers.get_app_config, stand_config)
    container.provide(runtime.get_runtime_config, stand_config)
    container.provide(runtime.get_raw_config, stand_raw)
    container.provide(runtime.app_name, AppName.CHAINLIT)
    container.provide(providers.storage_provider, storage)
    container.provide(providers.session_source, ChainlitSessions(StandTokens()))
    container.provide(runtime.connection_types, runtime.connection_types())
    container.eager(runtime.users_table)
    container.eager(runtime.message_bus)
    container.eager(runtime.payload_store)
    container.eager(runtime.live_locks)
    container.eager(runtime.connection_store)
    container.eager(runtime.workflow_store)
    container.eager(runtime.kb_schema)
    container.eager(providers.chainlit_data_layer)

    return container


async def test_chainlit_components_share_one_pool(
    app_container: Container,
    stand_config: AppConfig,
) -> None:
    """Шина, блокировки, соединения, workflow, kb и слой данных — на общем пуле."""
    Container.set_root(app_container)

    try:
        await app_container.start()

        layer = app_container.resolved(providers.chainlit_data_layer)
        if not isinstance(layer, PostgresDataLayer):
            raise AssertionError(f"data layer provider gives a layer: {layer!r}")

        schemas = sorted(pool.search_path for pool in AsyncPostgresPool.opened())
        if schemas != ["", stand_config.checkpointer.db_schema]:
            raise AssertionError(
                "chainlit opens one shared pool plus the checkpointer one, "
                f"got pools on schemas {schemas}"
            )
    finally:
        Container.set_root(None)
        await app_container.aclose()


async def test_close_all_closes_pools_of_the_app(app_container: Container) -> None:
    """Остановка приложения снимает все пулы процесса, а не только кэшированные."""
    Container.set_root(app_container)

    try:
        await app_container.start()
        if not AsyncPostgresPool.opened():
            raise AssertionError("the container opens at least one pool")

        await AsyncPostgresPool.close_all()

        left = AsyncPostgresPool.opened()
        if left:
            schemas = sorted(pool.search_path for pool in left)
            raise AssertionError(
                f"close_all leaves no pool of the app open, got {schemas}"
            )
    finally:
        Container.set_root(None)
        await app_container.aclose()
