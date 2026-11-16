"""Отказы на реальном PostgreSQL: обрываются только соединения тестового стенда."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Sequence
from contextlib import asynccontextmanager
from enum import StrEnum
from typing import Any
from uuid import uuid4

import psycopg
import pytest

from boba.db.postgres import AsyncPostgresPool
from boba.tasks.models import (
    ExecutionContext,
    TaskCode,
    TaskCreate,
    TaskExecutor,
    TaskOutcome,
    TaskServiceError,
    TaskState,
    TaskStatus,
    WorkerConfig,
    WorkerIdentity,
)
from boba.tasks.service import TaskQuery, TaskRow, TaskService
from boba.tasks.worker import TaskWorker

pytestmark = [pytest.mark.anyio, pytest.mark.integration]


class DisconnectAt(StrEnum):
    BEFORE_WRITE = "before_write"
    BEFORE_COMMIT = "before_commit"
    AFTER_COMMIT = "after_commit"


class DisconnectingService(TaskService):
    """Исполняет рабочий SQL; обрывает соединение в заданной точке транзакции.

    AFTER_COMMIT теряет уже полученный ответ: проверяет сверку неизвестного исхода
    на реально сохранённой строке без вмешательства в сетевой трафик сервера.
    """

    def __init__(self, pool: AsyncPostgresPool) -> None:
        super().__init__(pool)
        self.queries: frozenset[TaskQuery] = frozenset()
        self.phase = DisconnectAt.BEFORE_WRITE
        self.remaining = 0
        self.until = 0.0
        self.tripped = asyncio.Event()

    def arm(
        self,
        query: TaskQuery,
        phase: DisconnectAt,
        *,
        duration: float = 0,
        also: Sequence[TaskQuery] = (),
    ) -> None:
        self.queries = frozenset((query, *also))
        self.phase = phase
        self.remaining = 1
        self.until = asyncio.get_running_loop().time() + duration
        self.tripped.clear()

    def _fails(self, query: TaskQuery, phase: DisconnectAt) -> bool:
        if query not in self.queries:
            return False

        if phase is not self.phase:
            return False

        if self.remaining:
            self.remaining -= 1
            return True

        return asyncio.get_running_loop().time() < self.until

    async def _disconnect(self, connection: psycopg.AsyncConnection[Any]) -> None:
        async with self.pool.connection() as control:
            await control.execute(
                "select pg_terminate_backend(%s)", (connection.info.backend_pid,)
            )

        self.tripped.set()
        await connection.execute("select 1")
        raise AssertionError("The terminated test connection accepted a query")

    async def _execute(
        self,
        connection: psycopg.AsyncConnection[Any],
        query: TaskQuery,
        params: Sequence[Any],
    ) -> tuple[TaskRow, ...]:
        if self._fails(query, DisconnectAt.BEFORE_WRITE):
            await self._disconnect(connection)

        rows = await super()._execute(connection, query, params)
        if self._fails(query, DisconnectAt.BEFORE_COMMIT):
            await self._disconnect(connection)

        return rows

    async def _rows(
        self,
        query: TaskQuery,
        params: Sequence[Any],
        connection: psycopg.AsyncConnection[Any] | None = None,
    ) -> tuple[TaskRow, ...]:
        rows = await super()._rows(query, params, connection)
        if not self._fails(query, DisconnectAt.AFTER_COMMIT):
            return rows

        try:
            async with self.pool.connection() as disposable:
                await self._disconnect(disposable)

        except psycopg.Error as exc:
            raise TaskServiceError(
                TaskCode.STORAGE,
                f"Test lost the response after {query.value} commit: {exc}",
            ) from exc

        raise AssertionError("The test connection was not terminated")


class ControlledExecutor(TaskExecutor):
    """Считает реальные запуски и позволяет удержать действие до отказа БД."""

    def __init__(self) -> None:
        self.calls = 0
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.release.set()
        self.stopped = asyncio.Event()

    async def execute(self, task: TaskState, context: ExecutionContext) -> TaskOutcome:
        self.calls += 1
        self.started.set()
        try:
            await context.report_progress({"step": "started"})
            await self.release.wait()
            return TaskOutcome(status=TaskStatus.COMPLETED, result=task.payload)

        finally:
            self.stopped.set()


class FailureStand:
    def __init__(self, pool: AsyncPostgresPool, timeout: float) -> None:
        self.owner = "task-failures-" + uuid4().hex
        self.service = TaskService(pool)
        self.faults = DisconnectingService(pool)
        self.executor = ControlledExecutor()
        self.config = WorkerConfig(
            concurrency=1,
            lease_sec=0.6,
            heartbeat_sec=0.05,
            poll_sec=0.02,
            maintenance_sec=0.03,
            retry_sec=0.03,
            shutdown_sec=0.5,
        )
        self.identity = WorkerIdentity(
            uuid4().hex, frozenset({"failure-test"}), owners=frozenset({self.owner})
        )
        self.worker = TaskWorker(
            self.faults, self.identity, self.config, self.executor
        )
        self.timeout = timeout

    async def add(self) -> TaskState:
        return await self.service.add(
            self.owner,
            TaskCreate(
                task_id=uuid4().hex,
                kind="failure-test",
                payload={"text": "once"},
                run_timeout_sec=10,
            ),
        )

    @asynccontextmanager
    async def running(self) -> AsyncGenerator[None, None]:
        process = asyncio.create_task(self.worker.run())
        try:
            async with asyncio.timeout(self.timeout):
                yield

        finally:
            async with asyncio.timeout(self.timeout):
                await self.worker.stop()
                await process

    async def terminal(self, task_id: str) -> TaskState:
        while True:
            state = await self.service.status(self.owner, task_id)
            if state.status.terminal:
                return state

            await asyncio.sleep(0.02)


@pytest.fixture
async def failures(history, agent_timeout) -> AsyncGenerator[FailureStand, None]:
    stand = FailureStand(history.pool, agent_timeout)
    try:
        yield stand

    finally:
        async with history.pool.connection() as connection:
            await connection.execute(
                "delete from boba.tasks where owner_id = %s", (stand.owner,)
            )


@pytest.mark.parametrize("phase", tuple(DisconnectAt))
@pytest.mark.parametrize("query", (TaskQuery.CLAIM, TaskQuery.FINISH))
async def test_lost_transition_is_reconciled_without_reexecution(
    failures: FailureStand, query: TaskQuery, phase: DisconnectAt
) -> None:
    initial = await failures.add()
    failures.faults.arm(query, phase)
    async with failures.running():
        state = await failures.terminal(initial.task_id)
        await failures.worker.stop()

    assert failures.faults.tripped.is_set()
    assert failures.executor.calls == 1
    assert state.status is TaskStatus.COMPLETED
    assert state.result == initial.payload
    assert state.revision == 4


async def test_finish_retry_keeps_renewing_lease(failures: FailureStand) -> None:
    initial = await failures.add()
    failures.faults.arm(
        TaskQuery.FINISH, DisconnectAt.BEFORE_WRITE, duration=1.2
    )
    async with failures.running():
        state = await failures.terminal(initial.task_id)

    assert state.status is TaskStatus.COMPLETED
    assert failures.executor.calls == 1
    assert state.finished_at is not None
    assert state.started_at is not None
    assert (state.finished_at - state.started_at).total_seconds() > 0.6


async def test_finish_outage_past_lease_expires_without_reexecution(
    failures: FailureStand,
) -> None:
    initial = await failures.add()
    failures.executor.release.clear()
    async with failures.running():
        await failures.executor.started.wait()
        failures.faults.arm(
            TaskQuery.FINISH,
            DisconnectAt.BEFORE_WRITE,
            duration=1.2,
            also=(TaskQuery.RENEW,),
        )
        failures.executor.release.set()
        state = await failures.terminal(initial.task_id)
        await failures.worker.stop()

    assert state.status is TaskStatus.INTERRUPTED
    assert state.result is None
    assert failures.executor.calls == 1


@pytest.mark.parametrize("phase", tuple(DisconnectAt))
async def test_progress_disconnect_does_not_abort_execution(
    failures: FailureStand, phase: DisconnectAt
) -> None:
    initial = await failures.add()
    failures.faults.arm(TaskQuery.PROGRESS, phase)
    async with failures.running():
        state = await failures.terminal(initial.task_id)

    assert state.status is TaskStatus.COMPLETED
    assert state.progress == {"step": "started"}
    assert failures.executor.calls == 1


async def test_renew_disconnect_recovers_before_expiry(failures: FailureStand) -> None:
    initial = await failures.add()
    failures.faults.arm(TaskQuery.RENEW, DisconnectAt.BEFORE_WRITE, duration=0.15)
    async with failures.running():
        state = await failures.terminal(initial.task_id)

    assert state.status is TaskStatus.COMPLETED
    assert failures.executor.calls == 1


async def test_lost_heartbeat_stops_action_and_expires(failures: FailureStand) -> None:
    initial = await failures.add()
    failures.executor.release.clear()
    async with failures.running():
        await failures.executor.started.wait()
        failures.faults.arm(
            TaskQuery.RENEW, DisconnectAt.BEFORE_WRITE, duration=2
        )
        await failures.executor.stopped.wait()
        state = await failures.terminal(initial.task_id)

    assert state.status is TaskStatus.INTERRUPTED
    assert state.error is not None
    assert state.error.code == "lease_expired"
    assert failures.executor.calls == 1


async def test_blocked_renewal_cancels_before_database_unlock(
    failures: FailureStand,
) -> None:
    initial = await failures.add()
    failures.executor.release.clear()
    async with failures.running():
        await failures.executor.started.wait()
        while True:
            state = await failures.service.status(failures.owner, initial.task_id)
            if state.progress is not None:
                break

            await asyncio.sleep(0.01)

        async with failures.service.pool.connection() as locker:
            async with locker.transaction():
                await locker.execute(
                    "select task_id from boba.tasks where task_id = %s for update",
                    (initial.task_id,),
                )
                async with asyncio.timeout(2):
                    await failures.executor.stopped.wait()

        state = await failures.terminal(initial.task_id)

    assert state.status is TaskStatus.INTERRUPTED
    assert failures.executor.calls == 1


@pytest.mark.parametrize("operation", (TaskQuery.FINISH, TaskQuery.RENEW))
async def test_expiry_races_reject_stale_owner(
    failures: FailureStand, operation: TaskQuery
) -> None:
    initial = await failures.add()
    lease = await failures.service.claim(failures.identity, uuid4(), 0.05)
    assert lease is not None
    await asyncio.sleep(0.08)
    outcome = TaskOutcome(status=TaskStatus.COMPLETED, result="late")
    match operation:
        case TaskQuery.FINISH:
            update = failures.service.finish(lease, outcome)

        case TaskQuery.RENEW:
            update = failures.service.renew(lease, 1)

        case _:
            raise AssertionError(operation)

    result, _ = await asyncio.gather(
        update, failures.service.expire(), return_exceptions=True
    )
    assert isinstance(result, TaskServiceError)
    assert result.code is TaskCode.LEASE_LOST
    # expire мог пропустить строку, занятую конкурирующим UPDATE.
    await failures.service.expire()
    state = await failures.service.status(failures.owner, initial.task_id)
    assert state.status is TaskStatus.INTERRUPTED
    assert state.revision == 3


@pytest.mark.parametrize(
    "operation", (TaskQuery.FINISH, TaskQuery.RENEW, TaskQuery.PROGRESS)
)
async def test_write_waiting_for_row_lock_rechecks_expiry(
    failures: FailureStand, operation: TaskQuery
) -> None:
    initial = await failures.add()
    lease = await failures.service.claim(failures.identity, uuid4(), 0.15)
    assert lease is not None
    match operation:
        case TaskQuery.FINISH:
            update = failures.service.finish(
                lease, TaskOutcome(status=TaskStatus.COMPLETED, result="late")
            )

        case TaskQuery.RENEW:
            update = failures.service.renew(lease, 1)

        case TaskQuery.PROGRESS:
            update = failures.service.progress(lease, {"late": True})

        case _:
            raise AssertionError(operation)

    async with failures.service.pool.connection() as locker:
        async with locker.transaction():
            await locker.execute(
                "select task_id from boba.tasks where task_id = %s for update",
                (initial.task_id,),
            )
            pending = asyncio.create_task(update)
            await asyncio.sleep(0.25)

    with pytest.raises(TaskServiceError) as error:
        await pending

    assert error.value.code is TaskCode.LEASE_LOST
