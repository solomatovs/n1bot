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
from boba.aid.infra import providers, services
from boba.aid.infra.config import AppConfig
from boba.aid.infra.di import Container
from boba.aid.infra.messaging_backends import StandardMessagingBackends
from boba.aid.infra.session import ChainlitSessions
from boba.aid.infra.turns import StaleTurnCloser
from boba.chat.messages import ChatMessages
from boba.config import bind
from boba.db.postgres import AsyncPostgresPool
from boba.identity.context import CallContexts
from boba.messaging.config import AppName
from boba.stand.migrate import StandMigrations
from boba.stand.overlay import ConfigOverlay

pytestmark = pytest.mark.anyio


@pytest.fixture(autouse=True)
async def no_pools() -> AsyncIterator[None]:
    """Счёт идёт с нуля: пулы процессные и переживают тест, который их открыл."""
    await AsyncPostgresPool.close_all()
    yield
    await AsyncPostgresPool.close_all()


@pytest.fixture
def stand_raw(config_overlay: ConfigOverlay, test_database: str) -> DictConfig:
    """Конфиг стенда, у которого общий [postgres] смотрит в тестовую базу."""
    return config_overlay.tree({"postgres.dbname": test_database})


@pytest.fixture
def stand_config(stand_raw: DictConfig) -> AppConfig:
    return bind(stand_raw, path="app", model=AppConfig)


@pytest.fixture
async def app_container(
    stand_raw: DictConfig,
    stand_config: AppConfig,
    storage: object,
) -> Container:
    """Провайдеры aid, которым нужен postgres; тяжёлое подставлено готовым."""
    migrations = StandMigrations()
    await migrations.application(
        stand_config.postgres, stand_config.db_schema, "boba.aid"
    )

    container = Container(level="app")
    container.provide(providers.get_app_config, stand_config)
    container.provide(services.application_config, stand_config)
    container.provide(services.app_name, AppName("aid"))
    container.provide(services.message_codec, ChatMessages().codec)
    container.provide(
        services.messaging_backends, StandardMessagingBackends().registry
    )
    container.provide(services.stale_observer, StaleTurnCloser)
    container.provide(services.call_contexts, CallContexts())
    container.provide(providers.storage_provider, storage)
    container.provide(providers.session_source, ChainlitSessions(StandTokens()))
    container.eager(services.users_table)
    container.eager(services.message_bus)
    container.eager(services.payload_store)
    container.eager(services.live_locks)
    container.eager(providers.mcp_server_store)
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
        pool = app_container.resolved(services.postgres_pool)
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
