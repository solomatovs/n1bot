"""Описывает блокировки живых областей — кто ведёт тред или запуск прямо сейчас — и
порт для их захвата, подтверждения и снятия.

Ошибки:
LockBusyError — область занята живым держателем; в модели LockBusy — кто, зачем и
    сколько молчит.
LockLostError — блокировка держателя отобрана: протухла или снята сторожем.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from abc import abstractmethod
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import ClassVar, Protocol
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field

from boba.cancellation import RunCancellation, StopReason
from boba.identity.context import Scope
from boba.identity.errors import RefusalError

__all__ = [
    "LiveLock",
    "LiveLocks",
    "LiveLocksColumn",
    "LockBusy",
    "LockBusyError",
    "LockKeeper",
    "LockLostError",
    "LockMode",
    "LockPurpose",
    "LockRefusal",
    "LockToken",
    "MemoryLiveLocks",
    "RunLocking",
    "StaleLock",
]


class LockMode(StrEnum):
    """Режим блокировки: ход и запуск берут область монопольно, уборка — разделяемо."""

    EXCLUSIVE = "exclusive"
    SHARED = "shared"


class LockPurpose(StrEnum):
    """Ради чего область занята; по нему строится текст отказа для пользователя."""

    TURN = "turn"
    RUN = "run"
    TOOL_CALL = "tool_call"
    CLEANUP = "cleanup"

    def describe(self) -> str:
        if self is LockPurpose.TURN:
            return "is running a turn"

        if self is LockPurpose.RUN:
            return "is running the workflow"

        if self is LockPurpose.TOOL_CALL:
            return "is running a tool call"

        return "is cleaning up"


class LockRefusal(StrEnum):
    """Виды отказов блокировок, которые уходят в RefusalError.kind."""

    BUSY = "scope_busy"


class LockToken(BaseModel):
    """Fencing-token держателя области: публикация и запись состояния предъявляют
    его, доказывая, что блокировка ещё принадлежит держателю.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    value: UUID

    @classmethod
    def local(cls) -> LockToken:
        """Выпускает token без блокировки для сообщений, которым держатель не нужен."""
        return cls(value=uuid4())


class LiveLocksColumn(StrEnum):
    """Колонки live_locks: кто, зачем и в каком режиме держит область и когда
    подтверждал жизнь.
    """

    SCOPE_KIND = "scope_kind"
    SCOPE_ID = "scope_id"
    MODE = "mode"
    HOLDER = "holder"
    TOKEN = "token"  # noqa: S105
    PURPOSE = "purpose"
    USER_ID = "user_id"
    ACQUIRED_AT = "acquired_at"
    HEARTBEAT_AT = "heartbeat_at"
    TTL_SEC = "ttl_sec"


class LiveLock(BaseModel):
    """Захваченная блокировка: область, режим, назначение, держатель, token и срок
    жизни.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    scope: Scope
    mode: LockMode
    purpose: LockPurpose
    holder: str = Field(min_length=1)
    token: LockToken
    ttl_sec: int = Field(gt=0)


class LockBusy(BaseModel):
    """Описывает держателя, который мешает захвату: кто, в каком режиме, зачем и
    сколько секунд молчит.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    holder: str = Field(min_length=1)
    mode: LockMode
    purpose: LockPurpose
    silent_sec: int = Field(ge=0)

    def describe(self, scope: Scope) -> str:
        return (
            f"{scope.kind.value} is busy: {self.holder} {self.purpose.describe()} "
            f"(last seen {self.silent_sec}s ago)"
        )


class StaleLock(BaseModel):
    """Протухшая блокировка, снятая сторожем: область, держатель и назначение."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    scope: Scope
    holder: str
    purpose: LockPurpose


class LockBusyError(RefusalError):
    """Отказ захвата: область занята живым держателем, описанным в busy."""

    def __init__(self, scope: Scope, busy: LockBusy) -> None:
        super().__init__(LockRefusal.BUSY, busy.describe(scope))
        self.scope = scope
        self.busy = busy


class LockLostError(Exception):
    """Блокировка держателя отобрана: протухла или снята сторожем."""


class LiveLocks(Protocol):
    """Порт блокировок областей: захват, подтверждение жизни, освобождение, список
    держателей и уборка протухших.
    """

    SYSTEM_USER: ClassVar[UUID] = UUID(int=0)
    """user_id захвата от имени приложения: уборка ходов умерших держателей."""

    @abstractmethod
    async def acquire(
        self, scope: Scope, mode: LockMode, purpose: LockPurpose, user_id: UUID
    ) -> LiveLock:
        """Захватывает область в режиме mode; занятую живым держателем отвергает
        LockBusyError.
        """

    @abstractmethod
    async def heartbeat(self, token: LockToken) -> bool:
        """Подтверждает жизнь блокировки; False означает, что её уже нет."""

    @abstractmethod
    async def release(self, token: LockToken) -> None:
        """Снимает блокировку по token; отсутствующая блокировка не ошибка."""

    @abstractmethod
    async def release_all(self, holder: str) -> int:
        """Снимает все блокировки инстанса при его остановке и возвращает их число."""

    @abstractmethod
    async def holders_of(self, scope: Scope) -> Sequence[LockBusy]:
        """Возвращает живых держателей области."""

    @abstractmethod
    async def reap(self) -> Sequence[StaleLock]:
        """Снимает протухшие блокировки всех областей и возвращает их."""


Clock = Callable[[], float]


@dataclass(frozen=True)
class RunLocking:
    """Чем держатель ведёт область: порт блокировок и период подтверждения жизни."""

    locks: LiveLocks
    heartbeat_sec: float


logger = logging.getLogger(__name__)


class LockKeeper:
    """Фоновая задача держателя: подтверждает жизнь блокировки раз в heartbeat_sec, а
    потеряв её, отменяет ход через RunCancellation.
    """

    def __init__(
        self,
        locks: LiveLocks,
        lock: LiveLock,
        cancellation: RunCancellation,
        heartbeat_sec: float,
    ) -> None:
        self._locks = locks
        self._lock = lock
        self._cancellation = cancellation
        self._heartbeat_sec = heartbeat_sec
        self._task: asyncio.Task[None] | None = None

    async def __aenter__(self) -> LockKeeper:
        self._task = asyncio.create_task(
            self._run(), name=f"lock-keeper:{self._lock.scope.render()}"
        )
        return self

    async def __aexit__(self, *exc: object) -> None:
        task = self._task
        self._task = None
        if task is None:
            return

        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

        await self._locks.release(self._lock.token)

    async def _run(self) -> None:
        failing_since: float | None = None
        loop = asyncio.get_running_loop()
        while True:
            await asyncio.sleep(self._heartbeat_sec)
            try:
                alive = await self._locks.heartbeat(self._lock.token)
            except Exception:
                if failing_since is None:
                    failing_since = loop.time()

                logger.warning(
                    "heartbeat of %s failed", self._lock.scope.render(), exc_info=True
                )
                if loop.time() - failing_since < self._lock.ttl_sec:
                    continue

                alive = False

            if alive:
                failing_since = None
                continue

            logger.error(
                "lock lost: %s held by %s", self._lock.scope.render(), self._lock.holder
            )
            self._cancellation.cancel(StopReason.LOCK_LOST)
            return


class MemoryLiveLocks(LiveLocks):
    """Блокировки в памяти процесса с подменяемыми часами для тестов и стенда без
    базы.
    """

    def __init__(
        self, holder: str, ttl_sec: int, clock: Clock = time.monotonic
    ) -> None:
        self._holder = holder
        self._ttl_sec = ttl_sec
        self._clock = clock
        self._locks: dict[LockToken, LiveLock] = {}
        self._beats: dict[LockToken, float] = {}

    def _stale(self, token: LockToken) -> bool:
        return self._beats[token] + self._locks[token].ttl_sec < self._clock()

    def _drop_stale(self, scope: Scope) -> None:
        for token, lock in list(self._locks.items()):
            if lock.scope != scope:
                continue

            if self._stale(token):
                self._forget(token)

    def _forget(self, token: LockToken) -> None:
        self._locks.pop(token, None)
        self._beats.pop(token, None)

    def _busy(self, lock: LiveLock) -> LockBusy:
        silent = int(self._clock() - self._beats[lock.token])
        return LockBusy(
            holder=lock.holder, mode=lock.mode, purpose=lock.purpose, silent_sec=silent
        )

    async def acquire(
        self, scope: Scope, mode: LockMode, purpose: LockPurpose, user_id: UUID
    ) -> LiveLock:
        self._drop_stale(scope)

        for lock in self._locks.values():
            if lock.scope != scope:
                continue

            if mode is LockMode.EXCLUSIVE:
                raise LockBusyError(scope, self._busy(lock))

            if lock.mode is LockMode.EXCLUSIVE:
                raise LockBusyError(scope, self._busy(lock))

        lock = LiveLock(
            scope=scope,
            mode=mode,
            purpose=purpose,
            holder=self._holder,
            token=LockToken.local(),
            ttl_sec=self._ttl_sec,
        )
        self._locks[lock.token] = lock
        self._beats[lock.token] = self._clock()
        return lock

    async def heartbeat(self, token: LockToken) -> bool:
        if token not in self._locks:
            return False

        if self._stale(token):
            self._forget(token)
            return False

        self._beats[token] = self._clock()
        return True

    async def release(self, token: LockToken) -> None:
        self._forget(token)

    async def release_all(self, holder: str) -> int:
        mine = [t for t, lock in self._locks.items() if lock.holder == holder]
        for token in mine:
            self._forget(token)

        return len(mine)

    async def holders_of(self, scope: Scope) -> Sequence[LockBusy]:
        self._drop_stale(scope)
        return [
            self._busy(lock) for lock in self._locks.values() if lock.scope == scope
        ]

    async def reap(self) -> Sequence[StaleLock]:
        stale: list[StaleLock] = []
        for token, lock in list(self._locks.items()):
            if not self._stale(token):
                continue

            stale.append(
                StaleLock(scope=lock.scope, holder=lock.holder, purpose=lock.purpose)
            )
            self._forget(token)

        return stale

    def holds(self, token: LockToken) -> bool:
        """Проверяет, жива ли блокировка с этим token; заменяет fencing шине в
        памяти.
        """
        if token not in self._locks:
            return False

        return not self._stale(token)
