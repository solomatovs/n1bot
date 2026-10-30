"""Пул postgres процесса aid: все таблицы живут на одном пуле, который
открывает провайдер postgres_pool в корне процесса.

Контейнер собирается как в bootstrap, но на тестовой базе, и тест считает пулы
процесса: таблицы ставят схему в запрос, своих пулов у них нет.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from aid_stand import StandTokens
from omegaconf import DictConfig

from boba.aid.data.data_layer import PostgresDataLayer
from boba.aid.infra import providers
from boba.aid.infra.config import AppConfig
from boba.aid.infra.session import ChainlitSessions
from boba.config import bind
from boba.connections.sealed import SealKeys
from boba.db.postgres import AsyncPostgresPool
from boba.identity.context import CallContexts
from boba.runtime import providers as runtime
from boba.runtime.config import AppName
from boba.runtime.di import Container
from boba.stand.overlay import ConfigOverlay

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
    return ConfigOverlay(raw_config).merged({"postgres.dbname": test_database})


@pytest.fixture
def stand_config(stand_raw: DictConfig) -> AppConfig:
    return bind(stand_raw, path="app", model=AppConfig)


@pytest.fixture
def app_container(
    stand_raw: DictConfig,
    stand_config: AppConfig,
    storage: object,
) -> Container:
    """Провайдеры aid, которым нужен postgres; тяжёлое подставлено готовым."""
    container = Container(level="app")
    container.provide(providers.get_app_config, stand_config)
    container.provide(runtime.get_runtime_config, stand_config)
    container.provide(runtime.get_raw_config, stand_raw)
    container.provide(runtime.app_name, AppName.AID)
    container.provide(providers.storage_provider, storage)
    container.provide(providers.session_source, ChainlitSessions(StandTokens()))
    container.provide(runtime.connection_types, runtime.connection_types())
    container.provide(runtime.seal_keys, SealKeys())
    container.provide(runtime.call_contexts, CallContexts())
    container.eager(providers.harness_storage)
    container.eager(runtime.users_table)
    container.eager(runtime.message_bus)
    container.eager(runtime.payload_store)
    container.eager(runtime.live_locks)
    container.eager(runtime.connection_store)
    container.eager(providers.chainlit_data_layer)

    return container


async def test_aid_components_share_one_pool(app_container: Container) -> None:
    """Шина, блокировки, соединения и слой данных — на пуле процесса."""
    Container.set_root(app_container)

    try:
        await app_container.start()

        layer = app_container.resolved(providers.chainlit_data_layer)
        if not isinstance(layer, PostgresDataLayer):
            raise AssertionError(f"data layer provider gives a layer: {layer!r}")

        opened = AsyncPostgresPool.opened()
        pool = app_container.resolved(runtime.postgres_pool)
        if len(opened) != 1:
            raise AssertionError(f"aid opens one pool, got {len(opened)} pools")

        if opened[0] is not pool:
            raise AssertionError("the open pool is not the pool of postgres_pool")
    finally:
        Container.set_root(None)
        await app_container.aclose()


async def test_container_close_closes_the_pool(app_container: Container) -> None:
    """Закрытие контейнера приложения снимает пул процесса."""
    Container.set_root(app_container)

    try:
        await app_container.start()
        if not AsyncPostgresPool.opened():
            raise AssertionError("the container opens the pool of the process")
    finally:
        Container.set_root(None)
        await app_container.aclose()

    left = AsyncPostgresPool.opened()
    if left:
        raise AssertionError(f"closing the container leaves {len(left)} pools open")
