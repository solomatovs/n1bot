"""Остановка хода: флаг отмены и прерыватели длинных операций."""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Generator
from contextlib import contextmanager
from contextvars import ContextVar

logger = logging.getLogger(__name__)

__all__ = [
    "ToolStopped",
    "TurnCancellation",
    "current_cancellation",
    "turn_cancellation",
]


class ToolStopped(BaseException):
    """Инструмент прерван остановкой; BaseException — мимо except Exception."""


class TurnCancellation:
    """Флаг остановки хода и прерыватели (proc.kill, conn.cancel, close)."""

    def __init__(self) -> None:
        self._event = threading.Event()
        self._lock = threading.Lock()
        self._aborts: dict[int, Callable[[], None]] = {}
        self._next_id = 0

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def cancel(self) -> None:
        with self._lock:
            if self._event.is_set():
                return
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
    def abort_with(self, abort: Callable[[], None]) -> Generator[None]:
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
def turn_cancellation() -> Generator[TurnCancellation]:
    "открывает ход: публикует свежий TurnCancellation в контексте"
    cancellation = TurnCancellation()
    token = _CURRENT.set(cancellation)
    try:
        yield cancellation
    finally:
        _CURRENT.reset(token)
