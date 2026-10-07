"""Остановка запуска: флаг отмены, прерыватели и публикация отмены в контексте.

Отмена работает в двух мирах сразу. Синхронный код (песочница, libpq, http)
прерывается зарегистрированными прерывателями, асинхронный — отменой задачи
запуска, которую владелец регистрирует как прерыватель. Реестр активных
запусков живёт у владельца (Runs приложения), здесь — только примитивы.

ProcessStop — остановка процесса по сигналу, общая для всех приложений.

Ошибки:
ToolStopped — работа прервана остановкой запуска.
"""

from __future__ import annotations

import asyncio
import logging
import signal
import threading
from collections.abc import Callable, Coroutine, Generator
from contextlib import contextmanager
from contextvars import ContextVar
from enum import StrEnum
from types import FrameType
from typing import ClassVar, TypeAlias

logger = logging.getLogger(__name__)

__all__ = [
    "ProcessStop",
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
    with RunCancellation().published() as cancellation:
        yield cancellation


StopAction: TypeAlias = Callable[[signal.Signals], None]
"""Действие остановки процесса: получает пришедший сигнал."""


class ProcessStop:
    """Остановка процесса по SIGTERM и SIGINT, общая для всех приложений.

    Обработчик сигнала по умолчанию убивает процесс на месте: незакрытые
    пулы, брошенные дочерние процессы, код выхода 143. Этот объект принимает
    сигнал сам и в цикле событий зовёт действия остановки, которые назвал
    владелец (on_stop): погасить запуски, закрыть сервер, остановить пул.
    Дальше процесс доходит до конца своего кода и выходит с кодом 0.
    Создаётся входом процесса. Приложение-задание отдаёт свою работу в
    run(): по сигналу она отменяется, её finally выполняются. Сервер uvicorn
    берёт объект в конструктор (SignalledServer) и сообщает ему о сигнале,
    пойманном самим uvicorn. Действия зовутся один раз, по первому сигналу.
    """

    HANDLED: ClassVar[tuple[signal.Signals, ...]] = (signal.SIGTERM, signal.SIGINT)

    def __init__(self) -> None:
        self._actions: list[StopAction] = []
        self._loops: list[asyncio.AbstractEventLoop] = []
        self._tasks: list[asyncio.Future[None]] = []
        self._received: list[signal.Signals] = []

    def on_stop(self, action: StopAction) -> None:
        """Действие, которое выполнится в цикле событий по сигналу остановки."""
        self._actions.append(action)

    def install(self) -> None:
        """Ставит обработчики сигналов; зовётся внутри цикла событий."""
        self._loops.append(asyncio.get_running_loop())
        for handled in self.HANDLED:
            signal.signal(handled, self._taken)

    def deliver(self, received: int) -> None:
        """Сигнал остановки, пойманный чужим обработчиком в любом потоке."""
        # обработчик сигнала прерывает главный поток в произвольном месте:
        # действия исполняет цикл событий, а не сам обработчик
        for loop in self._loops:
            loop.call_soon_threadsafe(self._stop, signal.Signals(received))

    async def run(self, work: Coroutine[object, object, None]) -> None:
        """Исполняет работу приложения до конца либо до сигнала остановки."""
        self.install()
        task = asyncio.ensure_future(work)
        self._tasks.append(task)
        try:
            await task
        except asyncio.CancelledError:
            if not self._received:
                raise

            logger.info("the work was stopped by %s", self._received[0].name)

    def _taken(self, received: int, frame: FrameType | None) -> None:
        self.deliver(received)

    def _stop(self, received: signal.Signals) -> None:
        if self._received:
            return

        self._received.append(received)
        logger.info(
            "stop signal %s: running %d stop action(s), stopping %d task(s)",
            received.name,
            len(self._actions),
            len(self._tasks),
        )
        for action in self._actions:
            action(received)

        for task in self._tasks:
            task.cancel()
