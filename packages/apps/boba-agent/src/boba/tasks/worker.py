"""Исполнение и наблюдение задач PostgreSQL без зависимости от агента.

Ошибки:
TaskServiceError — нарушение контракта worker или остановка слушателя.
ExceptionGroup — необработанная ошибка служебного цикла; worker останавливается.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from contextlib import suppress
from enum import StrEnum
from functools import partial
from typing import Any
from uuid import uuid4

import psycopg
from pydantic import JsonValue

from boba.db.postgres.errors import PostgresError
from boba.tasks.models import (
    ExecutionContext,
    TaskChanged,
    TaskCode,
    TaskError,
    TaskExecutionError,
    TaskExecutor,
    TaskLease,
    TaskOutcome,
    TaskServiceError,
    TaskStatus,
    WorkerConfig,
    WorkerIdentity,
)
from boba.tasks.service import TaskService

logger = logging.getLogger(__name__)


class TaskChannel(StrEnum):
    READY = "boba_task_ready"
    CHANGED = "boba_task_changed"


class TaskNotifications:
    """Слушает каналы и пробуждает callbacks потребителей для чтения TaskService."""

    def __init__(
        self,
        connect: Callable[[], Awaitable[psycopg.AsyncConnection[Any]]],
        retry_sec: float = 1,
    ) -> None:
        self._connect = connect
        self._retry_sec = retry_sec
        self._subscribers: dict[str, set[Callable[[TaskChanged | None], None]]] = {}
        self._ready_subscribers: set[Callable[[], None]] = set()
        self._connected = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._connection: psycopg.AsyncConnection[Any] | None = None
        self._closed = False

    @property
    def backend_pid(self) -> int:
        if self._connection is None:
            raise TaskServiceError(TaskCode.LISTENER, "Task listener is not connected")

        return self._connection.info.backend_pid

    async def start(self) -> None:
        if self._task is not None:
            return

        self._closed = False
        self._connected.clear()
        self._task = asyncio.create_task(self._run(), name="task-notifications")
        await self.wait_connected()

    async def wait_connected(self) -> None:
        await self._connected.wait()
        if self._task is not None:
            if self._task.done():
                await self._task

    def subscribe(
        self, task_id: str, handler: Callable[[TaskChanged | None], None]
    ) -> Callable[[], None]:
        subscribers = self._subscribers.setdefault(task_id, set())
        subscribers.add(handler)
        return partial(self._unsubscribe, task_id, handler)

    def _unsubscribe(
        self, task_id: str, handler: Callable[[TaskChanged | None], None]
    ) -> None:
        subscribers = self._subscribers.get(task_id)
        if subscribers is None:
            return

        subscribers.discard(handler)
        if not subscribers:
            del self._subscribers[task_id]

    def subscribe_ready(self, handler: Callable[[], None]) -> Callable[[], None]:
        self._ready_subscribers.add(handler)
        return partial(self._ready_subscribers.discard, handler)

    async def _listen(self) -> None:
        async with await self._connect() as connection:
            self._connection = connection
            await connection.execute("listen boba_task_ready")
            await connection.execute("listen boba_task_changed")
            await connection.commit()
            self._connected.set()
            for callback in tuple(self._ready_subscribers):
                callback()

            for callbacks in tuple(self._subscribers.values()):
                for callback in tuple(callbacks):
                    callback(None)

            async for notification in connection.notifies():
                self._dispatch(notification)

    def _dispatch(self, notification: psycopg.Notify) -> None:
        change = TaskChanged.model_validate_json(notification.payload)
        channel = TaskChannel(notification.channel)
        if channel is TaskChannel.READY:
            for callback in tuple(self._ready_subscribers):
                callback()

            return

        for handler in tuple(self._subscribers.get(change.task_id, ())):
            handler(change)

    async def _run(self) -> None:
        try:
            while not self._closed:
                try:
                    await self._listen()

                except (psycopg.Error, PostgresError, OSError) as exc:
                    if self._closed:
                        return

                    logger.warning("Task listener disconnected; reconnecting: %s", exc)

                finally:
                    self._connection = None
                    self._connected.clear()

                await asyncio.sleep(self._retry_sec)

        except asyncio.CancelledError:
            raise

        except Exception as exc:
            raise TaskServiceError(
                TaskCode.LISTENER, f"Task listener stopped: {exc}"
            ) from exc

        finally:
            self._connected.set()

    async def close(self) -> None:
        self._closed = True
        if self._task is None:
            return

        if self._connection is not None:
            await self._connection.close()

        self._task.cancel()
        with suppress(asyncio.CancelledError):
            await self._task

        self._task = None


class TaskWorker:
    """Ведёт исполнение и lease; run_maintenance обслуживает сроки без исполнителя."""

    def __init__(
        self,
        service: TaskService,
        identity: WorkerIdentity,
        config: WorkerConfig,
        executor: TaskExecutor | None = None,
        notifications: TaskNotifications | None = None,
    ) -> None:
        self.service = service
        self.identity = identity
        self.config = config
        self.executor = executor
        self.notifications = notifications
        self._stopping = asyncio.Event()
        self._wake = asyncio.Event()
        self._finished = asyncio.Event()
        self._finished.set()
        self._running: set[asyncio.Task[None]] = set()

    def _start(self) -> None:
        if not self._finished.is_set():
            raise TaskServiceError(TaskCode.CONFLICT, "Task worker is already running")

        self._stopping.clear()
        self._finished.clear()

    async def stop(self) -> None:
        self._stopping.set()
        self._wake.set()
        await self._finished.wait()

    async def _pause(self, seconds: float) -> None:
        try:
            async with asyncio.timeout(seconds):
                await self._stopping.wait()

        except TimeoutError:
            return

    async def run_maintenance(self) -> None:
        self._start()
        try:
            await self._maintenance()

        finally:
            self._finished.set()

    async def _maintenance(self) -> None:
        while not self._stopping.is_set():
            try:
                expired = await self.service.expire(self.config.maintenance_batch)
                if len(expired) == self.config.maintenance_batch:
                    continue

            except TaskServiceError as exc:
                if exc.code is not TaskCode.STORAGE:
                    raise

                logger.warning("Task expiry scan failed; retrying: %s", exc)

            await self._pause(self.config.maintenance_sec)

    async def run(self) -> None:
        if self.executor is None:
            raise TaskServiceError(
                TaskCode.CONFLICT, "Task worker requires an executor"
            )

        if not self.identity.kinds:
            raise TaskServiceError(
                TaskCode.CONFLICT, "Task worker must declare its supported kinds"
            )

        self._start()
        unsubscribe = None
        if self.notifications is not None:
            unsubscribe = self.notifications.subscribe_ready(self._wake.set)

        try:
            await self._initial_expiry()
            async with asyncio.TaskGroup() as group:
                group.create_task(self._maintenance(), name="task-maintenance")
                group.create_task(self._claim_loop(), name="task-claims")

        finally:
            self._stopping.set()
            if unsubscribe is not None:
                unsubscribe()

            try:
                await self._drain()

            finally:
                self._finished.set()

    async def _initial_expiry(self) -> None:
        while not self._stopping.is_set():
            try:
                await self.service.expire(self.config.maintenance_batch)
                return

            except TaskServiceError as exc:
                if exc.code is not TaskCode.STORAGE:
                    raise

                logger.warning(
                    "Worker %s: initial expiry scan failed: %s",
                    self.identity.worker_id,
                    exc,
                )
                await self._pause(self.config.retry_sec)

    async def _claim_loop(self) -> None:
        token = uuid4()
        while not self._stopping.is_set():
            self._wake.clear()
            self._collect()
            if len(self._running) >= self.config.concurrency:
                await self._wait_for_work()
                continue

            try:
                lease = await self.service.claim(
                    self.identity, token, self.config.lease_sec
                )

            except TaskServiceError as exc:
                if exc.code is not TaskCode.STORAGE:
                    raise

                logger.warning(
                    "Worker %s: claim failed; recovering token %s: %s",
                    self.identity.worker_id,
                    token,
                    exc,
                )
                await self._pause(self.config.retry_sec)
                continue

            token = uuid4()
            if lease is None:
                await self._wait_for_work()
                continue

            if lease.state.status.terminal:
                continue

            action = asyncio.create_task(
                self._execute(lease), name=f"task:{lease.state.task_id}"
            )
            self._running.add(action)
            action.add_done_callback(self._completed)

    def _completed(self, task: asyncio.Task[None]) -> None:
        self._wake.set()

    def _collect(self) -> None:
        for task in tuple(self._running):
            if not task.done():
                continue

            self._running.remove(task)
            task.result()

    async def _wait_for_work(self) -> None:
        try:
            async with asyncio.timeout(self.config.poll_sec):
                await self._wake.wait()

        except TimeoutError:
            return

    async def _heartbeat(self, lease: TaskLease) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + lease.remaining_sec
        delay = min(self.config.heartbeat_sec, lease.remaining_sec)
        while True:
            await asyncio.sleep(max(delay, 0))
            try:
                lease = await self.service.renew(lease, self.config.lease_sec)

            except TaskServiceError as exc:
                if exc.code is not TaskCode.STORAGE:
                    raise

                remaining = deadline - loop.time()
                if remaining <= 0:
                    raise TaskServiceError(
                        TaskCode.LEASE_LOST,
                        f"Task {lease.state.task_id}: "
                        "lease could not be renewed before expiry",
                    ) from exc

                logger.warning(
                    "Task %s: heartbeat unavailable; retrying: %s",
                    lease.state.task_id,
                    exc,
                )
                delay = min(self.config.retry_sec, remaining)
                continue

            deadline = loop.time() + lease.remaining_sec
            delay = min(self.config.heartbeat_sec, lease.remaining_sec)

    async def _progress(self, lease: TaskLease, value: JsonValue) -> None:
        await self.service.progress(lease, value)

    async def _execute(self, lease: TaskLease) -> None:
        try:
            lease = await self.service.renew(lease, self.config.lease_sec)
            outcome = await self._invoke(lease)
            await self._finish(lease, outcome)

        except TaskServiceError as exc:
            if exc.code not in (TaskCode.STORAGE, TaskCode.LEASE_LOST):
                raise

            logger.warning(
                "Task %s: execution ownership ended: %s", lease.state.task_id, exc
            )

    async def _invoke(self, lease: TaskLease) -> TaskOutcome:
        if self.executor is None:
            raise TaskServiceError(
                TaskCode.CONFLICT, "Task worker requires an executor"
            )

        context = ExecutionContext(partial(self._progress, lease))
        action = asyncio.create_task(self.executor.execute(lease.state, context))
        heartbeat = asyncio.create_task(self._heartbeat(lease))
        try:
            done, _ = await asyncio.wait(
                (action, heartbeat), return_when=asyncio.FIRST_COMPLETED
            )
            if heartbeat in done:
                await heartbeat

            return await action

        except TaskExecutionError as exc:
            return TaskOutcome(status=TaskStatus.FAILED, error=exc.error)

        except TaskServiceError:
            raise

        except asyncio.CancelledError:
            return TaskOutcome(
                status=TaskStatus.INTERRUPTED,
                error=TaskError(
                    code=TaskCode.INTERRUPTED,
                    message="Worker stopped; external completion is not confirmed.",
                ),
            )

        except Exception as exc:
            return TaskOutcome(
                status=TaskStatus.INTERRUPTED,
                error=TaskError(
                    code=TaskCode.EXECUTION,
                    message=f"Task executor failed with an unconfirmed outcome: {exc}",
                ),
            )

        finally:
            heartbeat.cancel()
            action.cancel()
            with suppress(asyncio.CancelledError):
                await heartbeat

            await self._stop_action(action)

    async def _stop_action(self, action: asyncio.Task[TaskOutcome]) -> None:
        done, _ = await asyncio.wait((action,), timeout=self.config.shutdown_sec)
        if not done:
            logger.error("Task executor ignored cancellation; its lease will expire")
            return

        with suppress(asyncio.CancelledError, TaskExecutionError, TaskServiceError):
            exception = action.exception()
            if exception is not None:
                logger.error("Task executor stopped with error: %s", exception)

    async def _finish(self, lease: TaskLease, outcome: TaskOutcome) -> None:
        while True:
            try:
                await self.service.finish(lease, outcome)
                return

            except TaskServiceError as exc:
                if exc.code is not TaskCode.STORAGE:
                    raise

                if self._stopping.is_set():
                    logger.error(
                        "Task %s: final state could not be saved during shutdown: %s",
                        lease.state.task_id,
                        exc,
                    )
                    return

                logger.warning(
                    "Task %s: saving outcome failed; reconciling commit: %s",
                    lease.state.task_id,
                    exc,
                )
                await self._pause(self.config.retry_sec)

    async def _drain(self) -> None:
        if not self._running:
            return

        _, pending = await asyncio.wait(self._running, timeout=self.config.shutdown_sec)
        for task in pending:
            task.cancel()

        await asyncio.gather(*self._running)
        self._running.clear()
