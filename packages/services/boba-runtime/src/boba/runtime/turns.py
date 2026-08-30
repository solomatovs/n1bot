"""Закрывает ходы чата, чей держатель умер: сторож нашёл протухшую блокировку turn, а
ход в шине остался открытым.

Ошибки:
MessageBusError — шина не отдала сохранённые сообщения или не приняла закрытие.
LockBusyError — область уже занята новым держателем: закрывать нечего.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence

from boba.identity.context import Scope
from boba.identity.locks import (
    LiveLocks,
    LockBusyError,
    LockMode,
    LockPurpose,
    StaleLock,
)
from boba.messaging import (
    AnswerInterrupted,
    Envelope,
    MessageBus,
    MessageKind,
    TurnFinished,
    TurnOutcome,
    TurnStarted,
)

__all__ = ["StaleTurnCloser"]

logger = logging.getLogger(__name__)


class StaleTurnCloser:
    """Находит по сохранённым сообщениям открытый ход области и публикует его
    завершение под блокировкой уборки, чтобы ленты на живых инстансах закрыли ход.
    """

    def __init__(self, bus: MessageBus, locks: LiveLocks) -> None:
        self._bus = bus
        self._locks = locks

    async def close(self, stale: Sequence[StaleLock]) -> int:
        """Закрывает открытые ходы областей протухших блокировок turn и возвращает их
        число.
        """
        closed = 0
        for lock in stale:
            if lock.purpose is not LockPurpose.TURN:
                continue

            if await self._close_scope(lock.scope, lock.holder):
                closed += 1

        return closed

    async def _close_scope(self, scope: Scope, holder: str) -> bool:
        started = self._open_turn(await self._bus.replay(scope, 0))
        if started is None:
            return False

        try:
            lock = await self._locks.acquire(
                scope, LockMode.EXCLUSIVE, LockPurpose.CLEANUP, LiveLocks.SYSTEM_USER
            )
        except LockBusyError:
            logger.info("stale turn of %s is already taken over", scope.render())
            return False

        try:
            note = TurnFinished.HOLDER_GONE
            await self._bus.publish(
                scope,
                AnswerInterrupted(turn_id=started.turn_id, key=started.key, note=note),
                lock.token,
            )
            await self._bus.publish(
                scope,
                TurnFinished(
                    turn_id=started.turn_id, outcome=TurnOutcome.STOPPED, reason=note
                ),
                lock.token,
            )
        finally:
            await self._locks.release(lock.token)

        logger.warning(
            "turn %s of %s closed: holder %s is gone",
            started.turn_id,
            scope.render(),
            holder,
        )
        return True

    @staticmethod
    def _open_turn(stored: Sequence[Envelope]) -> TurnStarted | None:
        """Возвращает последний TurnStarted, за которым нет TurnFinished; None —
        открытого хода нет.
        """
        started: TurnStarted | None = None
        for envelope in stored:
            message = envelope.message
            if isinstance(message, TurnStarted):
                started = message
                continue

            if message.kind is MessageKind.TURN_FINISHED:
                started = None

        return started
