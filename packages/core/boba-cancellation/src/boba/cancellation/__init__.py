"""Остановка хода: флаг отмены, прерыватели и публикация отмены в контексте.

Отмена работает в двух мирах сразу. Синхронный код (песочница, libpq, http)
прерывается зарегистрированными прерывателями, асинхронный — отменой задачи
хода, которую владелец хода регистрирует как прерыватель. Реестр активных
ходов живёт у владельца (TurnContext чата), здесь — только примитивы.

Ошибки: ToolStopped — работа прервана остановкой хода.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Generator
from contextlib import contextmanager
from contextvars import ContextVar
from enum import StrEnum

logger = logging.getLogger(__name__)

__all__ = [
    "StopReason",
    "ToolStopped",
    "TurnCancellation",
    "current_cancellation",
    "turn_cancellation",
]


class StopReason(StrEnum):
    """Почему ход остановлен; текст для пользователя выбирает интерфейс."""

    USER_STOP = "user_stop"
    ABORTED = "aborted"
    SUPERSEDED = "superseded"
    FAILED = "failed"


class ToolStopped(BaseException):
    """Инструмент прерван остановкой; BaseException — мимо except Exception."""


class TurnCancellation:
    """Флаг остановки хода и прерыватели (proc.kill, conn.cancel, task.cancel)."""

    def __init__(self) -> None:
        self._event = threading.Event()
        self._lock = threading.Lock()
        self._aborts: dict[int, Callable[[], None]] = {}
        self._next_id = 0
        self._reason: StopReason | None = None

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    @property
    def reason(self) -> StopReason | None:
        """Причина остановки; None — ход не останавливали."""
        return self._reason

    def cancel(self, reason: StopReason = StopReason.USER_STOP) -> None:
        with self._lock:
            if self._event.is_set():
                return
            self._reason = reason
            self._event.set()
            aborts = list(self._aborts.values())
        for abort in aborts:
            try:
                abort()
            except Exception:
                logger.exception("operation interrupter failed")

    def raise_if_cancelled(self) -> None:
        if self._event.is_set():
            raise ToolStopped

    def wait(self, timeout: float) -> bool:
        "ждёт отмены не дольше timeout; True — ход остановлен"
        return self._event.wait(timeout)

    @contextmanager
    def abort_with(self, abort: Callable[[], None]) -> Generator[None, None, None]:
        "регистрирует прерыватель на время блока и проверяет отмену на входе"
        self.raise_if_cancelled()
        with self._lock:
            key = self._next_id
            self._next_id += 1
            self._aborts[key] = abort
        try:
            yield
        finally:
            with self._lock:
                self._aborts.pop(key, None)


_NEVER_CANCELLED = TurnCancellation()

_CURRENT: ContextVar[TurnCancellation] = ContextVar(
    "boba_turn_cancellation",
    default=_NEVER_CANCELLED,
)


def current_cancellation() -> TurnCancellation:
    "отмена текущего хода; вне хода — объект, который никогда не отменяется"
    return _CURRENT.get()


@contextmanager
def turn_cancellation() -> Generator[TurnCancellation, None, None]:
    "открывает ход: публикует свежий TurnCancellation в контексте"
    cancellation = TurnCancellation()
    token = _CURRENT.set(cancellation)
    try:
        yield cancellation
    finally:
        _CURRENT.reset(token)
