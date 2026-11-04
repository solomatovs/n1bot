"""Готовые комплекты messaging в памяти и PostgreSQL, выбираемые приложением.

Ошибки:
MessageBusError — транспорт PostgreSQL недоступен.
LockStoreError — регистрация инстанса не выполнена.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Sequence
from contextlib import AbstractAsyncContextManager, AsyncExitStack, asynccontextmanager
from typing import Literal

from boba.core.components import ComponentFactory, Components
from boba.identity.locks import MemoryLiveLocks, StaleLock
from boba.messaging import MemoryMessageBus, MemoryPayloadStore
from boba.runtime.bus import PgMessageBus
from boba.runtime.config import MessagingConfig, RuntimeConfig
from boba.runtime.locks import LockReaper, PgLiveLocks
from boba.runtime.messaging import MessagingRuntime, MessagingServices, StaleObserver
from boba.runtime.payloads import PgPayloadStore


class LocalMessagingConfig(MessagingConfig):
    """Настройки обмена в памяти одного процесса."""

    provider: Literal["local"] = "local"


class PostgresMessagingConfig(MessagingConfig):
    """Настройки обмена через таблицы и LISTEN/NOTIFY PostgreSQL."""

    provider: Literal["postgres"] = "postgres"


class StandardMessagingBackends:
    """Предлагает приложению фабрики согласованных ресурсов messaging."""

    def __init__(self) -> None:
        self.registry: Components[
            MessagingConfig,
            MessagingServices,
            AbstractAsyncContextManager[MessagingRuntime],
        ] = Components(
            MessagingConfig,
            "provider",
            (
                ComponentFactory(LocalMessagingConfig, self.local),
                ComponentFactory(PostgresMessagingConfig, self.postgres),
            ),
        )

    @asynccontextmanager
    async def local(
        self, config: LocalMessagingConfig, services: MessagingServices
    ) -> AsyncIterator[MessagingRuntime]:
        locks = MemoryLiveLocks(services.instance, services.config.cluster.lock_ttl_sec)
        bus = MemoryMessageBus(services.instance, services.codec, locks.fence)
        yield MessagingRuntime(bus, MemoryPayloadStore(), locks)

    @asynccontextmanager
    async def postgres(
        self, config: PostgresMessagingConfig, services: MessagingServices
    ) -> AsyncIterator[MessagingRuntime]:
        process = services.config
        bus = PgMessageBus(
            process.postgres,
            process.db_schema,
            services.instance,
            process.cluster,
            services.codec,
            services.pool,
        )
        payloads = PgPayloadStore(process.postgres, process.db_schema, services.pool)
        locks = PgLiveLocks(
            process.postgres,
            process.db_schema,
            services.instance,
            services.app,
            process.cluster,
            services.pool,
        )
        observer = services.observer(bus, locks)
        maintenance = PostgresRetention(process, bus, payloads, observer)
        reaper = LockReaper(
            locks,
            process.cluster.reaper_period_sec,
            maintenance.on_stale,
            maintenance.on_sweep,
        )
        await locks.register_instance()
        async with AsyncExitStack() as cleanup:
            cleanup.push_async_callback(bus.stop)
            cleanup.push_async_callback(locks.release_all, services.instance)
            cleanup.push_async_callback(reaper.stop)
            await bus.start()
            await reaper.start()
            yield MessagingRuntime(bus, payloads, locks)


class PostgresRetention:
    """Проверяет очередь уведомлений и удаляет просроченные события и тела."""

    def __init__(
        self,
        config: RuntimeConfig,
        bus: PgMessageBus,
        payloads: PgPayloadStore,
        observer: StaleObserver,
    ) -> None:
        self._observer = observer
        self._config = config
        self._bus = bus
        self._payloads = payloads
        self._logger = logging.getLogger(__name__)

    async def on_stale(self, stale: Sequence[StaleLock]) -> None:
        closed = await self._observer.close(stale)
        if closed:
            self._logger.warning("Closed %d operation(s) of stale holders", closed)

    async def on_sweep(self) -> None:
        usage = await self._bus.queue_usage()
        if usage > self._config.cluster.queue_usage_limit:
            # долю очереди держит соединение слушателя: освобождает только разрыв
            self._logger.error(
                "notification queue usage %.3f exceeds %.3f: reconnecting the listener",
                usage,
                self._config.cluster.queue_usage_limit,
            )
            await self._bus.listener.reconnect()

        removed = await self._bus.purge_idle(self._config.cluster.retention_sec)
        bodies = await self._payloads.purge_idle(self._config.cluster.retention_sec)
        if removed or bodies:
            self._logger.info(
                "live retention: %d events, %d bodies removed", removed, bodies
            )
