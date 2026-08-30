"""Секция [messaging] выбирает реализацию шины процесса: local живёт в памяти,
postgres — в таблицах live_*; провайдеры контейнера отдают согласованный стек.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from boba.identity.context import Scope
from boba.identity.locks import MemoryLiveLocks
from boba.messaging import (
    AnswerToken,
    Envelope,
    ListenerState,
    LockToken,
    MemoryMessageBus,
    MemoryPayloadStore,
)
from boba.runtime import providers
from boba.runtime.config import AppName, LocalMessagingConfig, RuntimeConfig
from boba.runtime.di import Container

pytestmark = pytest.mark.anyio


@pytest.fixture
def local_config(runtime_config: RuntimeConfig) -> RuntimeConfig:
    """Конфиг приложения с [messaging] provider = local поверх стендового toml."""
    messaging = LocalMessagingConfig(provider="local")
    return runtime_config.model_copy(update={"messaging": messaging})


async def test_local_messaging_builds_the_memory_stack(
    local_config: RuntimeConfig,
) -> None:
    container = Container(level="app")
    container.provide(providers.get_runtime_config, local_config)
    container.provide(providers.app_name, AppName.STUDIO)
    container.eager(providers.message_bus)
    container.eager(providers.payload_store)
    container.eager(providers.live_locks)
    Container.set_root(container)

    try:
        await container.start()

        bus = container.resolved(providers.message_bus)
        assert isinstance(bus, MemoryMessageBus)

        payloads = container.resolved(providers.payload_store)
        assert isinstance(payloads, MemoryPayloadStore)

        locks = container.resolved(providers.live_locks)
        assert isinstance(locks, MemoryLiveLocks)

        watch = providers.bus_watch_ref()
        assert watch.state is ListenerState.LISTENING

        assert providers.message_bus_ref() is bus
    finally:
        Container.set_root(None)
        await container.aclose()


async def test_local_bus_delivers_between_container_consumers(
    local_config: RuntimeConfig,
) -> None:
    container = Container(level="app")
    container.provide(providers.get_runtime_config, local_config)
    container.provide(providers.app_name, AppName.STUDIO)
    container.eager(providers.message_bus)
    Container.set_root(container)

    try:
        await container.start()

        bus = providers.message_bus_ref()
        scope = Scope.chat(str(uuid4()))
        received: list[Envelope] = []

        async def take(envelope: Envelope) -> None:
            received.append(envelope)

        leave = bus.subscribe(scope, take)
        message = AnswerToken(turn_id="t1", key="m1", token="hello")
        seq = await bus.publish(scope, message, LockToken.local())

        assert seq == 1
        assert [envelope.seq for envelope in received] == [1]

        replayed = await bus.replay(scope, 0)
        assert [envelope.seq for envelope in replayed] == [1]

        leave()
    finally:
        Container.set_root(None)
        await container.aclose()
