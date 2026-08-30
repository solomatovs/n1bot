"""Контракт блокировок в памяти: монопольность, протухание, heartbeat, уборка."""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest

from boba.identity.context import Scope
from boba.identity.locks import (
    LockBusyError,
    LockMode,
    LockPurpose,
    MemoryLiveLocks,
)

pytestmark = pytest.mark.anyio


class Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


def _scope() -> Scope:
    return Scope.chat(str(uuid4()))


async def test_second_exclusive_holder_is_refused_with_the_first_described() -> None:
    clock = Clock()
    locks = MemoryLiveLocks("node1-chainlit", ttl_sec=20, clock=clock)
    scope = _scope()
    await locks.acquire(scope, LockMode.EXCLUSIVE, LockPurpose.TURN, UUID(int=7))
    clock.now += 3

    with pytest.raises(LockBusyError) as caught:
        await locks.acquire(
            scope, LockMode.EXCLUSIVE, LockPurpose.TOOL_CALL, UUID(int=7)
        )

    assert caught.value.busy.holder == "node1-chainlit"
    assert caught.value.busy.purpose is LockPurpose.TURN
    assert caught.value.busy.silent_sec == 3
    assert "is running a turn (last seen 3s ago)" in str(caught.value)


async def test_shared_holders_coexist_but_exclusive_waits_for_them() -> None:
    locks = MemoryLiveLocks("n", ttl_sec=20)
    scope = _scope()
    await locks.acquire(scope, LockMode.SHARED, LockPurpose.CLEANUP, UUID(int=1))
    await locks.acquire(scope, LockMode.SHARED, LockPurpose.CLEANUP, UUID(int=2))

    with pytest.raises(LockBusyError):
        await locks.acquire(scope, LockMode.EXCLUSIVE, LockPurpose.TURN, UUID(int=1))

    exclusive_scope = _scope()
    await locks.acquire(
        exclusive_scope, LockMode.EXCLUSIVE, LockPurpose.RUN, UUID(int=1)
    )
    with pytest.raises(LockBusyError):
        await locks.acquire(
            exclusive_scope, LockMode.SHARED, LockPurpose.CLEANUP, UUID(int=1)
        )


async def test_stale_lock_is_taken_over_and_its_heartbeat_fails() -> None:
    clock = Clock()
    locks = MemoryLiveLocks("n", ttl_sec=20, clock=clock)
    scope = _scope()
    first = await locks.acquire(scope, LockMode.EXCLUSIVE, LockPurpose.RUN, UUID(int=1))
    clock.now += 21

    second = await locks.acquire(
        scope, LockMode.EXCLUSIVE, LockPurpose.RUN, UUID(int=2)
    )

    assert second.token != first.token
    assert await locks.heartbeat(first.token) is False
    assert await locks.heartbeat(second.token) is True
    assert locks.holds(second.token)
    assert not locks.holds(first.token)


async def test_release_and_release_all_free_the_scope() -> None:
    locks = MemoryLiveLocks("n", ttl_sec=20)
    scope = _scope()
    lock = await locks.acquire(scope, LockMode.EXCLUSIVE, LockPurpose.TURN, UUID(int=1))
    await locks.release(lock.token)
    await locks.acquire(scope, LockMode.EXCLUSIVE, LockPurpose.TURN, UUID(int=1))

    assert await locks.release_all("n") == 1
    assert await locks.holders_of(scope) == []


async def test_reap_returns_stale_locks_only() -> None:
    clock = Clock()
    locks = MemoryLiveLocks("n", ttl_sec=20, clock=clock)
    old = _scope()
    fresh = _scope()
    await locks.acquire(old, LockMode.EXCLUSIVE, LockPurpose.RUN, UUID(int=1))
    clock.now += 15
    await locks.acquire(fresh, LockMode.EXCLUSIVE, LockPurpose.TURN, UUID(int=1))
    clock.now += 6

    stale = await locks.reap()

    assert [s.scope for s in stale] == [old]
    assert stale[0].purpose is LockPurpose.RUN
    assert len(await locks.holders_of(fresh)) == 1
