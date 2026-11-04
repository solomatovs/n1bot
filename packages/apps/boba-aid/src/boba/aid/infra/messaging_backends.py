"""Готовые комплекты messaging в памяти и PostgreSQL, выбираемые приложением.

Ошибки:
MessageBusError — транспорт PostgreSQL недоступен.
LockStoreError — регистрация инстанса не выполнена.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncGenerator, Awaitable, Callable, Sequence
from contextlib import AbstractAsyncContextManager, AsyncExitStack, asynccontextmanager
from functools import partial
from typing import ClassVar, Literal

from boba.aid.infra.messaging import MessagingRuntime, MessagingServices, StaleObserver
from boba.core.components import ComponentFactory, Components
from boba.db.postgres import AsyncPostgresPool
from boba.db.postgres.messaging.bus import PgMessageBus
from boba.db.postgres.messaging.payloads import PayloadStoreError, PgPayloadStore
from boba.identity.locks import MemoryLiveLocks, StaleLock
from boba.identity_postgres.locks import LockStoreError, PgLiveLocks
from boba.messaging import MemoryMessageBus, MemoryPayloadStore, MessageBusError
from boba.messaging.config import ClusterConfig, MessagingConfig

logger = logging.getLogger(__name__)


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
    ) -> AsyncGenerator[MessagingRuntime, None]:
        locks = MemoryLiveLocks(services.instance, services.config.cluster.lock_ttl_sec)
        bus = MemoryMessageBus(services.instance, services.codec, locks.fence)
        yield MessagingRuntime(bus, MemoryPayloadStore(), locks)

    @asynccontextmanager
    async def postgres(
        self, config: PostgresMessagingConfig, services: MessagingServices
    ) -> AsyncGenerator[MessagingRuntime, None]:
        settings = services.config
        bus = PgMessageBus(
            services.pool,
            settings.db_schema,
            services.instance,
            settings.cluster,
            services.codec,
            listener_connect=partial(AsyncPostgresPool.dedicated, settings.postgres),
        )
        payloads = PgPayloadStore(services.pool, settings.db_schema)
        locks = PgLiveLocks(
            services.pool,
            settings.db_schema,
            services.instance,
            services.app,
            settings.cluster,
        )
        observer = services.observer(bus, locks)
        maintenance = PostgresRetention(settings.cluster, bus, payloads, observer)
        reaper = LockReaper(
            locks,
            settings.cluster.reaper_period_sec,
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
        cluster: ClusterConfig,
        bus: PgMessageBus,
        payloads: PgPayloadStore,
        observer: StaleObserver,
    ) -> None:
        self._observer = observer
        self._cluster = cluster
        self._bus = bus
        self._payloads = payloads
        self._logger = logging.getLogger(__name__)

    async def on_stale(self, stale: Sequence[StaleLock]) -> None:
        closed = await self._observer.close(stale)
        if closed:
            self._logger.warning("Closed %d operation(s) of stale holders", closed)

    async def on_sweep(self) -> None:
        usage = await self._bus.queue_usage()
        if usage > self._cluster.queue_usage_limit:
            # долю очереди держит соединение слушателя: освобождает только разрыв
            self._logger.error(
                "notification queue usage %.3f exceeds %.3f: reconnecting the listener",
                usage,
                self._cluster.queue_usage_limit,
            )
            await self._bus.listener.reconnect()

        removed = await self._bus.purge_idle(self._cluster.retention_sec)
        bodies = await self._payloads.purge_idle(self._cluster.retention_sec)
        if removed or bodies:
            self._logger.info(
                "live retention: %d events, %d bodies removed", removed, bodies
            )


StaleHandler = Callable[[Sequence[StaleLock]], Awaitable[None]]
SweepHandler = Callable[[], Awaitable[None]]


class LockReaper:
    """Периодически подтверждает жизнь своего инстанса, снимает протухшие блокировки
    и мёртвые инстансы и отдаёт снятые блокировки обработчику, который закрывает
    их ходы и запуски.
    """

    NAME: ClassVar[str] = "lock-reaper"

    def __init__(
        self,
        locks: PgLiveLocks,
        period_sec: float,
        on_stale: StaleHandler,
        on_sweep: SweepHandler,
    ) -> None:
        self._locks = locks
        self._period_sec = period_sec
        self._on_stale = on_stale
        self._on_sweep = on_sweep
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name=self.NAME)

    async def stop(self) -> None:
        task = self._task
        self._task = None
        if task is None:
            return

        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    async def sweep(self) -> Sequence[StaleLock]:
        """Выполняет один проход сторожа и возвращает снятые блокировки."""
        await self._locks.register_instance()
        stale = await self._locks.reap()
        if stale:
            await self._on_stale(stale)

        # мёртвые инстансы после снятия блокировок: каскад спрятал бы их от обработчика
        dead = await self._locks.reap_instances()
        if dead:
            logger.warning("dead instances removed: %s", ", ".join(dead))

        await self._on_sweep()
        return stale

    async def _run(self) -> None:
        # первый проход сразу: пульс инстанса не ждёт целый период после старта
        while True:
            try:
                await self.sweep()
            except (LockStoreError, MessageBusError, PayloadStoreError) as exc:
                logger.warning(
                    "lock reaper sweep failed, next try in %ds: %s",
                    self._period_sec,
                    exc,
                    exc_info=True,
                )

            await asyncio.sleep(self._period_sec)
