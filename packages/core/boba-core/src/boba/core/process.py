"""Подписка на сигналы процесса и доставка действий в event loop.

Ошибки:
ProcessSignalError — подписка, восстановление обработчика или действие сигнала
    не выполнены; ошибки фоновых действий передаются владельцу при выходе.
"""

from __future__ import annotations

import asyncio
import signal
import threading
from collections.abc import Awaitable, Callable, Iterable
from enum import StrEnum
from types import FrameType, TracebackType
from typing import Self, TypeAlias

SignalAction: TypeAlias = Callable[[signal.Signals], None]
AsyncSignalAction: TypeAlias = Callable[[signal.Signals], Awaitable[None]]
SignalHandler: TypeAlias = Callable[[int, FrameType | None], object] | int


class ProcessSignalError(Exception):
    """Ошибка подписок или действий, доставляемая владельцу ProcessSignals."""


class SignalState(StrEnum):
    NEW = "new"
    ACTIVE = "active"
    CLOSING = "closing"
    CLOSED = "closed"


class _SignalReceiver:
    """Обработчик ОС, принадлежащий экземпляру ProcessSignals.

    Хранит только адрес доставки. После отсоединения не доставляет уже
    поставленные в очередь события и не затрагивает чужие подписки.
    """

    def __init__(self, deliver: SignalAction) -> None:
        self._deliver = deliver
        self._loop: asyncio.AbstractEventLoop | None = None

    def attach(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def detach(self) -> None:
        self._loop = None

    def check_loop(self) -> None:
        if self._loop is None:
            raise ProcessSignalError(
                "accessing signal subscriptions: owner is inactive"
            )

        try:
            current = asyncio.get_running_loop()
        except RuntimeError as exc:
            msg = "accessing signal subscriptions: expected the owning event loop"
            raise ProcessSignalError(msg) from exc

        if current is not self._loop:
            raise ProcessSignalError("accessing signal subscriptions: wrong event loop")

    def __call__(self, received: int, frame: FrameType | None) -> None:
        loop = self._loop
        if loop is None:
            return

        loop.call_soon_threadsafe(self._queued, signal.Signals(received))

    def _queued(self, received: signal.Signals) -> None:
        if self._loop is None:
            return

        self._deliver(received)


class _SignalWork:
    """Последовательные асинхронные действия с объединением повторных событий.

    Создаётся ProcessSignals и хранит задачи только своего экземпляра.
    Необработанные ошибки получает владелец через переданный обработчик.
    """

    def __init__(self, failed: Callable[[ProcessSignalError], None]) -> None:
        self._failed = failed
        self._actions: dict[signal.Signals, list[AsyncSignalAction]] = {}
        self._tasks: dict[signal.Signals, asyncio.Task[None]] = {}
        self._pending: set[signal.Signals] = set()
        self._accepting = True

    def register(self, received: signal.Signals, action: AsyncSignalAction) -> None:
        self._actions.setdefault(received, []).append(action)

    def signals(self) -> Iterable[signal.Signals]:
        return self._actions.keys()

    def request(self, received: signal.Signals) -> None:
        if not self._accepting:
            return

        if received not in self._actions:
            return

        self._pending.add(received)
        task = self._tasks.get(received)
        if task is not None:
            if not task.done():
                return

        task = asyncio.create_task(self._run(received), name=f"signal-{received.name}")
        self._tasks[received] = task
        task.add_done_callback(self._finished)

    async def _run(self, received: signal.Signals) -> None:
        try:
            while received in self._pending:
                self._pending.remove(received)
                for action in self._actions[received]:
                    await action(received)
        except Exception as exc:
            msg = f"handling {received.name}: asynchronous action failed: {exc}"
            raise ProcessSignalError(msg) from exc

    def _finished(self, task: asyncio.Task[None]) -> None:
        if task.cancelled():
            return

        failure = task.exception()
        if isinstance(failure, ProcessSignalError):
            self._failed(failure)

    def cancel(self) -> None:
        self._accepting = False
        self._pending.clear()
        for task in self._tasks.values():
            if task.done():
                continue

            if task.cancelling():
                continue

            task.cancel()

    async def close(self) -> None:
        self.cancel()
        tasks = tuple(self._tasks.values())
        if tasks:
            # Ошибки задач передаёт _finished; gather дожидается их освобождения.
            await asyncio.gather(*tasks, return_exceptions=True)


class ProcessSignals:
    """Владелец подписок, создаваемый сборкой процесса через ProcessStop.

    Принимает действия приложения до входа в async-контекст. Подписки
    устанавливаются в главном потоке и снимаются при выходе. Объект одноразовый.
    """

    def __init__(self) -> None:
        self._actions: dict[signal.Signals, list[SignalAction]] = {}
        self._previous: dict[signal.Signals, SignalHandler] = {}
        self._failures: list[ProcessSignalError] = []
        self._state = SignalState.NEW
        self._receiver = _SignalReceiver(self._dispatch)
        self._work = _SignalWork(self._failed)

    def on(self, received: signal.Signals, action: SignalAction) -> None:
        self._check_registration(received)
        self._actions.setdefault(received, []).append(action)

    def on_async(self, received: signal.Signals, action: AsyncSignalAction) -> None:
        self._check_registration(received)
        self._work.register(received, action)

    def _check_registration(self, received: signal.Signals) -> None:
        if threading.current_thread() is not threading.main_thread():
            msg = f"registering {received.name}: expected the main thread"
            raise ProcessSignalError(msg)

        if self._state is not SignalState.NEW:
            msg = f"registering {received.name}: expected new owner, got {self._state}"
            raise ProcessSignalError(msg)

        if received in (signal.SIGKILL, signal.SIGSTOP):
            raise ProcessSignalError(
                f"registering {received.name}: signal cannot be caught"
            )

    async def __aenter__(self) -> Self:
        if threading.current_thread() is not threading.main_thread():
            raise ProcessSignalError("installing signals: expected the main thread")

        if self._state is not SignalState.NEW:
            msg = f"installing signals: expected new owner, got {self._state}"
            raise ProcessSignalError(msg)

        selected = set(self._actions)
        selected.update(self._work.signals())
        for received in selected:
            previous = signal.getsignal(received)
            if isinstance(previous, _SignalReceiver):
                msg = f"installing {received.name}: another instance owns this signal"
                raise ProcessSignalError(msg)

            if previous is None:
                msg = (
                    f"installing {received.name}: cannot restore an unknown OS handler"
                )
                raise ProcessSignalError(msg)

        loop = asyncio.get_running_loop()
        self._receiver.attach(loop)
        try:
            for received in selected:
                previous = signal.signal(received, self._receiver)
                if previous is not None:
                    self._previous[received] = previous
        except Exception as exc:
            self._restore()
            raise ProcessSignalError(
                f"installing signal handlers failed: {exc}"
            ) from exc

        self._state = SignalState.ACTIVE
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self._receiver.check_loop()
        self._state = SignalState.CLOSING
        try:
            await self._work.close()
        finally:
            self._restore()

        if self._failures:
            causes = ExceptionGroup("signal actions failed", self._failures)
            details = map(str, self._failures)
            message = "; ".join(details)
            raise ProcessSignalError(
                f"handling process signals failed: {message}"
            ) from causes

    def cancel_pending(self) -> None:
        self._receiver.check_loop()
        self._work.cancel()

    def _restore(self) -> None:
        self._receiver.detach()
        self._state = SignalState.CLOSED
        conflicts: list[str] = []
        for received, previous in self._previous.items():
            if signal.getsignal(received) is not self._receiver:
                conflicts.append(received.name)
                continue

            signal.signal(received, previous)

        self._previous.clear()
        if conflicts:
            msg = f"restoring signals: handlers replaced by another owner: {conflicts}"
            raise ProcessSignalError(msg)

    def _dispatch(self, received: signal.Signals) -> None:
        for action in self._actions.get(received, ()):
            try:
                self._invoke(action, received)
            except ProcessSignalError as exc:
                self._failed(exc)

        self._work.request(received)

    def _invoke(self, action: SignalAction, received: signal.Signals) -> None:
        try:
            action(received)
        except Exception as exc:
            msg = f"handling {received.name}: synchronous action failed: {exc}"
            raise ProcessSignalError(msg) from exc

    def _failed(self, failure: ProcessSignalError) -> None:
        self._failures.append(failure)
        loop = asyncio.get_running_loop()
        loop.call_exception_handler({"message": str(failure), "exception": failure})
