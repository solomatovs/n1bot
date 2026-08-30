"""Блокировки на Postgres: гонка за область, протухание, heartbeat, fencing шины."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Sequence
from uuid import UUID, uuid4

import pytest
from psycopg import sql

from boba.db.postgres import AsyncPostgresPool
from boba.identity.context import Scope
from boba.identity.locks import (
    LockBusyError,
    LockLostError,
    LockMode,
    LockPurpose,
    LockToken,
    StaleLock,
)
from boba.messaging import RunStateChanged
from boba.runtime.bus import PgMessageBus
from boba.runtime.config import AppName, ClusterConfig, RuntimeConfig
from boba.runtime.locks import LockReaper, PgLiveLocks
from boba.runtime.tables import ChatTable

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

TTL_SEC = 2


def _cluster(runtime_config: RuntimeConfig) -> ClusterConfig:
    return runtime_config.cluster.model_copy(
        update={"lock_ttl_sec": TTL_SEC, "heartbeat_sec": 1, "reaper_period_sec": 1}
    )


class Stand:
    """Шина и блокировки одного инстанса над тестовой базой."""

    def __init__(self, bus: PgMessageBus, locks: PgLiveLocks) -> None:
        self.bus = bus
        self.locks = locks


async def _stand(
    runtime_config: RuntimeConfig, test_database: str, pool: AsyncPostgresPool, name: str
) -> Stand:
    cfg = runtime_config.data_layer.postgres.model_copy(update={"dbname": test_database})
    cluster = _cluster(runtime_config)
    bus = PgMessageBus(
        cfg, runtime_config.cluster.db_schema, name, AppName.STUDIO, cluster
    )
    bus._pool_ref = pool
    await bus.setup()
    await bus.start()
    locks = PgLiveLocks(
        cfg, runtime_config.cluster.db_schema, name, AppName.STUDIO, cluster
    )
    locks._pool_ref = pool
    await locks.register_instance()
    return Stand(bus, locks)


@pytest.fixture
async def stands(
    runtime_config: RuntimeConfig, test_database: str, pool: AsyncPostgresPool
) -> AsyncIterator[tuple[Stand, Stand]]:
    first = await _stand(runtime_config, test_database, pool, "node1-studio")
    second = await _stand(runtime_config, test_database, pool, "node2-studio")
    try:
        yield first, second
    finally:
        await first.bus.stop()
        await second.bus.stop()


async def test_only_one_of_concurrent_holders_gets_the_scope(
    stands: tuple[Stand, Stand],
) -> None:
    first, second = stands
    scope = Scope.workflow(uuid4())

    results = await asyncio.gather(
        first.locks.acquire(scope, LockMode.EXCLUSIVE, LockPurpose.RUN, UUID(int=1)),
        second.locks.acquire(scope, LockMode.EXCLUSIVE, LockPurpose.RUN, UUID(int=1)),
        return_exceptions=True,
    )

    refused = [r for r in results if isinstance(r, LockBusyError)]
    assert len(refused) == 1
    assert refused[0].busy.holder in ("node1-studio", "node2-studio")
    holders = await first.locks.holders_of(scope)
    assert [h.purpose for h in holders] == [LockPurpose.RUN]


async def test_shared_and_exclusive_matrix(stands: tuple[Stand, Stand]) -> None:
    first, second = stands
    scope = Scope.chat(str(uuid4()))
    await first.locks.acquire(scope, LockMode.SHARED, LockPurpose.CLEANUP, UUID(int=1))
    await second.locks.acquire(scope, LockMode.SHARED, LockPurpose.CLEANUP, UUID(int=2))

    with pytest.raises(LockBusyError):
        await first.locks.acquire(scope, LockMode.EXCLUSIVE, LockPurpose.TURN, UUID(int=1))

    assert len(await first.locks.holders_of(scope)) == 2


async def test_stale_lock_expires_by_ttl_and_lost_heartbeat_returns_false(
    stands: tuple[Stand, Stand],
) -> None:
    first, second = stands
    scope = Scope.chat(str(uuid4()))
    lock = await first.locks.acquire(scope, LockMode.EXCLUSIVE, LockPurpose.TURN, UUID(int=1))

    with pytest.raises(LockBusyError):
        await second.locks.acquire(scope, LockMode.EXCLUSIVE, LockPurpose.TURN, UUID(int=2))

    await asyncio.sleep(TTL_SEC + 0.3)

    taken = await second.locks.acquire(scope, LockMode.EXCLUSIVE, LockPurpose.TURN, UUID(int=2))
    assert taken.holder == "node2-studio"
    assert await first.locks.heartbeat(lock.token) is False
    assert await second.locks.heartbeat(taken.token) is True


async def test_publish_is_fenced_by_the_lock_token(stands: tuple[Stand, Stand]) -> None:
    first, _ = stands
    run_id = uuid4()
    scope = Scope.workflow(run_id)
    changed = RunStateChanged(run_id=run_id, status="running")

    with pytest.raises(LockLostError):
        await first.bus.publish(scope, changed, LockToken.local())

    lock = await first.locks.acquire(scope, LockMode.EXCLUSIVE, LockPurpose.RUN, UUID(int=1))
    assert await first.bus.publish(scope, changed, lock.token) == 1

    await first.locks.release(lock.token)
    with pytest.raises(LockLostError):
        await first.bus.publish(scope, changed, lock.token)


async def test_reaper_removes_stale_locks_and_dead_instances(
    stands: tuple[Stand, Stand],
) -> None:
    first, second = stands
    scope = Scope.workflow(uuid4())
    await second.locks.acquire(scope, LockMode.EXCLUSIVE, LockPurpose.RUN, UUID(int=1))
    seen: list[StaleLock] = []

    async def on_stale(stale: Sequence[StaleLock]) -> None:
        seen.extend(stale)

    async def on_sweep() -> None:
        return None

    reaper = LockReaper(first.locks, 1.0, on_stale, on_sweep)
    assert scope not in [s.scope for s in await reaper.sweep()]
    seen.clear()

    await asyncio.sleep(TTL_SEC + 0.3)
    stale = await reaper.sweep()

    ours = [s for s in stale if s.scope == scope]
    assert len(ours) == 1
    assert ours[0].holder == "node2-studio"
    assert ours[0].purpose is LockPurpose.RUN
    assert ours[0] in seen
    assert await first.locks.holders_of(scope) == []


async def test_instance_registration_survives_a_postgres_restart(
    stands: tuple[Stand, Stand], pool: AsyncPostgresPool
) -> None:
    """После рестарта Postgres unlogged-таблица инстансов пуста: проход сторожа
    регистрирует инстанс заново, и захват блокировки снова возможен.
    """
    first, _ = stands
    instances = ChatTable.LIVE_INSTANCES.under(first.locks._schema)
    async with pool.cursor() as cur:
        await cur.execute(
            sql.SQL("truncate {instances} cascade").format(instances=instances)
        )

    async def on_stale(stale: Sequence[StaleLock]) -> None:
        return None

    async def on_sweep() -> None:
        return None

    await LockReaper(first.locks, 1.0, on_stale, on_sweep).sweep()

    scope = Scope.chat(str(uuid4()))
    lock = await first.locks.acquire(scope, LockMode.EXCLUSIVE, LockPurpose.TURN, UUID(int=1))
    assert lock.holder == first.locks.instance
    await first.locks.release(lock.token)
