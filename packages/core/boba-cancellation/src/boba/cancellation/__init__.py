"""Остановка запуска: флаг отмены, прерыватели и публикация отмены в контексте.

Отмена работает в двух мирах сразу. Синхронный код (песочница, libpq, http)
прерывается зарегистрированными прерывателями, асинхронный — отменой задачи
запуска, которую владелец регистрирует как прерыватель. Реестр активных
запусков живёт у владельца (Runs приложения), здесь — только примитивы.

ProcessStop — остановка процесса по сигналу, общая для всех приложений.

Ошибки:
ToolStopped — работа прервана остановкой запуска.
ProcessSignalError — подписка или обработка сигналов процесса не выполнены;
    при сбое действий остановки цепочка причин содержит ProcessStopError.
Ошибки переданной в ProcessStop.run работы и внешняя отмена передаются без изменений.
"""

from __future__ import annotations

import asyncio
import logging
import signal
import threading
from collections.abc import Callable, Coroutine, Generator, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from enum import StrEnum
from typing import TypeAlias

from boba.core.process import ProcessSignalError, ProcessSignals

logger = logging.getLogger(__name__)

__all__ = [
    "ProcessSignalError",
    "ProcessStop",
    "ProcessStopError",
    "RunCancellation",
    "StopAction",
    "StopReason",
    "ToolStopped",
    "current_cancellation",
    "run_cancellation",
]


class StopReason(StrEnum):
    """Почему запуск остановлен; текст для пользователя выбирает интерфейс."""

    USER_STOP = "user_stop"
    ABORTED = "aborted"
    SUPERSEDED = "superseded"
    FAILED = "failed"
    LOCK_LOST = "lock_lost"
    SHUTDOWN = "shutdown"


class ToolStopped(BaseException):
    """Инструмент прерван остановкой; BaseException — мимо except Exception."""


class RunCancellation:
    """Флаг остановки запуска и прерыватели (proc.kill, conn.cancel, task.cancel)."""

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
        """Причина остановки; None — запуск не останавливали."""
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
            except Exception as exc:
                logger.exception(
                    "cancel(%s): operation interrupter %r failed: %s",
                    reason,
                    abort,
                    exc,
                )

    def raise_if_cancelled(self) -> None:
        if self._event.is_set():
            raise ToolStopped(f"the run was stopped: {self._reason}")

    def wait(self, timeout: float) -> bool:
        "ждёт отмены не дольше timeout; True — запуск остановлен"
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

    @contextmanager
    def published(self) -> Generator[RunCancellation, None, None]:
        "публикует эту отмену в контексте исполнения на время блока"
        token = _CURRENT.set(self)
        try:
            yield self
        finally:
            _CURRENT.reset(token)


_NEVER_CANCELLED = RunCancellation()

_CURRENT: ContextVar[RunCancellation] = ContextVar(
    "boba_run_cancellation",
    default=_NEVER_CANCELLED,
)


def current_cancellation() -> RunCancellation:
    "отмена текущего запуска; вне запуска — объект, который никогда не отменяется"
    return _CURRENT.get()


@contextmanager
def run_cancellation() -> Generator[RunCancellation, None, None]:
    "открывает запуск: публикует свежую RunCancellation в контексте"
    cancellation = RunCancellation()
    with cancellation.published():
        yield cancellation


StopAction: TypeAlias = Callable[[signal.Signals], None]
"""Действие остановки процесса: получает пришедший сигнал."""


class ProcessStopError(Exception):
    """Причина ProcessSignalError при сбое действий остановки приложения.

    Исходные ошибки действий сохранены в __cause__ как ExceptionGroup.
    Вызывающий run() или контекст signals перехватывает ProcessSignalError.
    """


class ProcessStop:
    """Остановка работы по SIGTERM/SIGINT через общий ProcessSignals.

    Действия приложения выполняются один раз. run() владеет подписками для
    консольного задания; для HTTP их контекстом владеет SignalledServer.
    Дополнительные сигналы приложение регистрирует через signals.
    """

    def __init__(self) -> None:
        self.signals = ProcessSignals()
        self._handled = (signal.SIGTERM, signal.SIGINT)
        self._actions: list[StopAction] = []
        self._tasks: list[asyncio.Future[None]] = []
        self._received: signal.Signals | None = None

        for received in self._handled:
            self.signals.on(received, self._stop)

    @property
    def handled(self) -> Sequence[signal.Signals]:
        return self._handled

    def on_stop(self, action: StopAction) -> None:
        """Короткое действие остановки в event loop по первому сигналу."""
        self._actions.append(action)

    async def run(self, work: Coroutine[object, object, None]) -> None:
        """Работа до завершения либо отмены сигналом; finally работы выполняются.

        ProcessSignalError — ошибка подписок или действий сигналов, включая
        остановку. Ошибки work и внешняя отмена передаются без изменений,
        если завершение контекста сигналов прошло успешно.
        """
        try:
            async with self.signals:
                task = asyncio.ensure_future(work)
                self._tasks.append(task)
                try:
                    await task
                except asyncio.CancelledError:
                    if self._received is None:
                        raise

                    logger.info("the work was stopped by %s", self._received.name)
                finally:
                    self._tasks.remove(task)
        finally:
            work.close()

    def _stop(self, received: signal.Signals) -> None:
        if self._received is not None:
            return

        self._received = received
        self.signals.cancel_pending()
        logger.info(
            "stop signal %s: running %d stop action(s), stopping %d task(s)",
            received.name,
            len(self._actions),
            len(self._tasks),
        )
        failures: list[Exception] = []
        for action in tuple(self._actions):
            try:
                action(received)
            except Exception as exc:
                failures.append(exc)

        for task in self._tasks:
            task.cancel()

        if failures:
            causes = ExceptionGroup("stop actions failed", failures)
            details = map(str, failures)
            message = "; ".join(details)
            raise ProcessStopError(
                f"stopping on {received.name}: {message}"
            ) from causes
