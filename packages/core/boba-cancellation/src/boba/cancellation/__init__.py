"""Остановка хода: флаг отмены, прерыватели и реестр активных ходов.

Ход адресуется идентификатором треда: остановить его можно откуда угодно —
из обработчика кнопки, из обработчика разрыва связи, из фоновой задачи. Это
единственная точка отмены, других способов остановить ход быть не должно.

Отмена работает в двух мирах сразу. Синхронный код (песочница, libpq, http)
прерывается зарегистрированными прерывателями, асинхронный — отменой задачи
хода, которая тоже регистрируется как прерыватель.

Ошибки: ToolStopped — работа прервана остановкой хода.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Callable, Generator
from contextlib import contextmanager
from contextvars import ContextVar
from enum import StrEnum
from typing import ClassVar

logger = logging.getLogger(__name__)

__all__ = [
    "StopReason",
    "ToolStopped",
    "TurnCancellation",
    "TurnRegistry",
    "current_cancellation",
    "turn_cancellation",
]


class StopReason(StrEnum):
    """Почему ход остановлен; текст для пользователя выбирает интерфейс."""

    USER_STOP = "user_stop"
    DISCONNECT = "disconnect"
    SUPERSEDED = "superseded"


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


class TurnRegistry:
    """Активные ходы по thread_id: единственный способ дотянуться до отмены."""

    _INSTANCE: ClassVar[TurnRegistry | None] = None

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._turns: dict[str, TurnCancellation] = {}

    @classmethod
    def instance(cls) -> TurnRegistry:
        """Общий реестр процесса: ходы живут дольше отдельного обработчика."""
        if cls._INSTANCE is None:
            cls._INSTANCE = TurnRegistry()
        return cls._INSTANCE

    @contextmanager
    def open(self, thread_id: str) -> Generator[TurnCancellation]:
        """Открывает ход треда: публикует отмену в контексте и в реестре."""
        with turn_cancellation() as cancellation:
            self._register(thread_id, cancellation)
            try:
                with self._task_abort(cancellation):
                    yield cancellation
            finally:
                self._release(thread_id, cancellation)

    def stop(self, thread_id: str, reason: StopReason) -> bool:
        """Останавливает ход треда; False — останавливать нечего."""
        cancellation = self.active(thread_id)
        if cancellation is None:
            logger.info("stop requested for thread %s: no active turn", thread_id)
            return False
        logger.info("stopping turn of thread %s (%s)", thread_id, reason.value)
        cancellation.cancel(reason)
        return True

    def active(self, thread_id: str) -> TurnCancellation | None:
        with self._lock:
            return self._turns.get(thread_id)

    def _register(self, thread_id: str, cancellation: TurnCancellation) -> None:
        with self._lock:
            stale = self._turns.get(thread_id)
            self._turns[thread_id] = cancellation
        if stale is None:
            return
        # новый ход того же треда: предыдущий дорабатывать незачем
        logger.warning("thread %s already had an active turn; stopping it", thread_id)
        stale.cancel(StopReason.SUPERSEDED)

    def _release(self, thread_id: str, cancellation: TurnCancellation) -> None:
        with self._lock:
            if self._turns.get(thread_id) is cancellation:
                del self._turns[thread_id]

    @contextmanager
    def _task_abort(self, cancellation: TurnCancellation) -> Generator[None]:
        """Отмена задачи хода как прерыватель: асинхронный мир тоже обрывается."""
        abort = self._task_canceller()
        if abort is None:
            yield
            return
        with cancellation.abort_with(abort):
            yield

    @staticmethod
    def _task_canceller() -> Callable[[], None] | None:
        """Прерыватель зовут из чужого потока — задачу трогаем только через loop."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return None
        task = asyncio.current_task()
        if task is None:
            return None

        def cancel_task() -> None:
            loop.call_soon_threadsafe(task.cancel)

        return cancel_task


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
