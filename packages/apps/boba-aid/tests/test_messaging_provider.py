"""Секция [messaging] выбирает реализацию шины процесса: local живёт в памяти,
postgres — в таблицах live_*; провайдеры контейнера отдают согласованный стек.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from boba.aid.config.app import AppConfig
from boba.aid.config.models import LocalMessagingConfig
from boba.aid.infra import services
from boba.aid.infra.di import Container
from boba.aid.infra.messaging_backends import StandardMessagingBackends
from boba.aid.infra.turns import StaleTurnCloser
from boba.chat.messages import ChatMessages, Notice, NoticeLevel
from boba.db.postgres import AsyncPostgresPool
from boba.identity.context import CallContexts, Scope
from boba.identity.locks import MemoryLiveLocks
from boba.messaging import (
    Envelope,
    LockToken,
    MemoryMessageBus,
    MemoryPayloadStore,
)
from boba.messaging.config import AppName

pytestmark = pytest.mark.anyio


@pytest.fixture
def local_config(runtime_config: AppConfig) -> AppConfig:
    """Конфиг приложения с [messaging] provider = local поверх стендового toml."""
    messaging = LocalMessagingConfig(provider="local")
    return runtime_config.model_copy(update={"messaging": messaging})


async def test_local_messaging_builds_the_memory_stack(
    local_config: AppConfig,
    pool: AsyncPostgresPool,
) -> None:
    container = Container(level="app")
    container.provide(services.application_config, local_config)
    container.provide(services.app_name, AppName("aid"))
    container.provide(services.postgres_pool, pool)
    container.provide(services.message_codec, ChatMessages().codec)
    container.provide(services.messaging_backends, StandardMessagingBackends().registry)
    container.provide(services.stale_observer, StaleTurnCloser)
    container.provide(services.call_contexts, CallContexts())
    container.eager(services.message_bus)
    container.eager(services.payload_store)
    container.eager(services.live_locks)
    Container.set_root(container)

    try:
        await container.start()

        bus = container.resolved(services.message_bus)
        assert isinstance(bus, MemoryMessageBus)

        payloads = container.resolved(services.payload_store)
        assert isinstance(payloads, MemoryPayloadStore)

        locks = container.resolved(services.live_locks)
        assert isinstance(locks, MemoryLiveLocks)

        assert container.resolved(services.message_bus) is bus
    finally:
        Container.set_root(None)
        await container.aclose()


async def test_local_bus_delivers_between_container_consumers(
    local_config: AppConfig,
    pool: AsyncPostgresPool,
) -> None:
    container = Container(level="app")
    container.provide(services.application_config, local_config)
    container.provide(services.app_name, AppName("aid"))
    container.provide(services.postgres_pool, pool)
    container.provide(services.message_codec, ChatMessages().codec)
    container.provide(services.messaging_backends, StandardMessagingBackends().registry)
    container.provide(services.stale_observer, StaleTurnCloser)
    container.provide(services.call_contexts, CallContexts())
    container.eager(services.message_bus)
    Container.set_root(container)

    try:
        await container.start()

        bus = container.resolved(services.message_bus)
        scope = Scope.chat(str(uuid4()))
        received: list[Envelope] = []

        async def take(envelope: Envelope) -> None:
            received.append(envelope)

        leave = bus.subscribe(scope, take)
        message = Notice(level=NoticeLevel.INFO, text="hello")
        seq = await bus.publish(scope, message, LockToken.local())

        assert seq == 1
        assert [envelope.seq for envelope in received] == [1]

        replayed = await bus.replay(scope, 0)
        assert [envelope.seq for envelope in replayed] == [1]

        leave()
    finally:
        Container.set_root(None)
        await container.aclose()
