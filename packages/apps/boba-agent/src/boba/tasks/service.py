"""API таблицы задач; SQL не зависит от агентов или способа исполнения.

Ошибки:
TaskServiceError — ошибка PostgreSQL, конфликт добавления, отсутствие задачи
    или потеря lease.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from enum import StrEnum
from functools import cache
from importlib.resources import files
from math import isfinite
from typing import Any
from uuid import UUID

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from pydantic import AwareDatetime, JsonValue, ValidationError

from boba.db.postgres import AsyncPostgresPool
from boba.db.postgres.errors import PostgresError
from boba.tasks.models import (
    TaskCode,
    TaskCreate,
    TaskLease,
    TaskModel,
    TaskOutcome,
    TaskServiceError,
    TaskState,
    WorkerIdentity,
)


class TaskQuery(StrEnum):
    ADD = "add"
    STATUS = "status"
    RECOVER = "recover"
    CLAIM = "claim"
    RENEW = "renew"
    PROGRESS = "progress"
    FINISH = "finish"
    EXPIRE = "expire"

    @cache
    def text(self) -> bytes:
        return files("boba.tasks").joinpath("sql", f"{self.value}.sql").read_bytes()


class TaskRow(TaskModel):
    """Граница SQL; публичное состояние отделено от данных владельца исполнения."""

    state: TaskState
    worker_id: str | None
    lease_token: UUID | None
    lease_expires_at: AwareDatetime | None
    remaining_sec: float | None
    notified: bool | None = None
    ready: bool | None = None

    def lease(self) -> TaskLease:
        if self.worker_id is None:
            raise TaskServiceError(
                TaskCode.LEASE_LOST, f"Task {self.state.task_id}: no worker"
            )

        if self.lease_token is None:
            raise TaskServiceError(
                TaskCode.LEASE_LOST, f"Task {self.state.task_id}: no lease token"
            )

        if self.lease_expires_at is None:
            raise TaskServiceError(
                TaskCode.LEASE_LOST, f"Task {self.state.task_id}: no lease expiry"
            )

        if self.remaining_sec is None:
            raise TaskServiceError(
                TaskCode.LEASE_LOST,
                f"Task {self.state.task_id}: missing lease duration",
            )

        return TaskLease(
            self.state,
            self.worker_id,
            self.lease_token,
            self.lease_expires_at,
            self.remaining_sec,
        )


class TaskService:
    """SQL-операции: производители добавляют задачи, workers меняют состояние."""

    def __init__(self, pool: AsyncPostgresPool) -> None:
        self.pool = pool

    async def _execute(
        self,
        connection: psycopg.AsyncConnection[Any],
        query: TaskQuery,
        params: Sequence[Any],
    ) -> tuple[TaskRow, ...]:
        async with psycopg.AsyncRawCursor(connection, row_factory=dict_row) as cursor:
            await cursor.execute(query.text(), params)
            rows = await cursor.fetchall()

        values = map(TaskRow.model_validate, rows)
        return tuple(values)

    async def _rows(
        self,
        query: TaskQuery,
        params: Sequence[Any],
        connection: psycopg.AsyncConnection[Any] | None = None,
    ) -> tuple[TaskRow, ...]:
        try:
            if connection is not None:
                if (
                    connection.info.transaction_status
                    is not psycopg.pq.TransactionStatus.INTRANS
                ):
                    raise TaskServiceError(
                        TaskCode.INVALID,
                        "Adding a task with a shared connection "
                        "requires an active transaction",
                    )

                return await self._execute(connection, query, params)

            async with self.pool.connection() as owned, owned.transaction():
                return await self._execute(owned, query, params)

        except (
            psycopg.IntegrityError,
            psycopg.ProgrammingError,
            ValidationError,
        ) as exc:
            raise TaskServiceError(
                TaskCode.INVALID, f"Task SQL {query.value} violated its contract: {exc}"
            ) from exc

        except (psycopg.Error, PostgresError) as exc:
            raise TaskServiceError(
                TaskCode.STORAGE, f"Task SQL {query.value} failed: {exc}"
            ) from exc

    async def add(
        self,
        owner_id: str,
        command: TaskCreate,
        *,
        connection: psycopg.AsyncConnection[Any] | None = None,
    ) -> TaskState:
        if not owner_id:
            raise TaskServiceError(TaskCode.CONFLICT, "Adding task: owner_id is empty")

        params = (
            owner_id,
            command.task_id,
            command.kind,
            command.route_key,
            Jsonb(command.payload),
            command.run_timeout_sec,
        )
        rows = await self._rows(TaskQuery.ADD, params, connection)
        if rows:
            return rows[0].state

        rows = await self._rows(
            TaskQuery.STATUS, (owner_id, command.task_id), connection
        )
        if not rows:
            raise TaskServiceError(
                TaskCode.CONFLICT, f"Task {command.task_id}: ID is unavailable"
            )

        state = rows[0].state
        original = TaskCreate(
            task_id=state.task_id,
            kind=state.kind,
            route_key=state.route_key,
            payload=state.payload,
            run_timeout_sec=state.run_timeout_sec,
        )
        if original != command:
            raise TaskServiceError(
                TaskCode.CONFLICT,
                f"Task {command.task_id}: command conflicts with saved definition",
            )

        return state

    async def status(self, owner_id: str, task_id: str) -> TaskState:
        rows = await self._rows(TaskQuery.STATUS, (owner_id, task_id))
        if not rows:
            raise TaskServiceError(
                TaskCode.NOT_FOUND, f"Task {task_id}: not found for owner {owner_id}"
            )

        return rows[0].state

    async def recover(self, worker: WorkerIdentity, token: UUID) -> TaskLease | None:
        rows = await self._rows(TaskQuery.RECOVER, (worker.worker_id, token))
        if not rows:
            return None

        lease = rows[0].lease()
        if not worker.permits(lease.state):
            raise TaskServiceError(
                TaskCode.LEASE_LOST,
                f"Task {lease.state.task_id}: worker permissions changed",
            )

        return lease

    async def claim(
        self, worker: WorkerIdentity, token: UUID, lease_sec: float
    ) -> TaskLease | None:
        self._duration(lease_sec)
        recovered = await self.recover(worker, token)
        if recovered is not None:
            return recovered

        rows = await self._rows(
            TaskQuery.CLAIM,
            (
                worker.worker_id,
                token,
                list(worker.kinds),
                list(worker.routes),
                list(worker.owners),
                worker.all_owners,
                lease_sec,
            ),
        )
        if not rows:
            return None

        return rows[0].lease()

    async def renew(self, lease: TaskLease, lease_sec: float) -> TaskLease:
        self._duration(lease_sec)
        rows = await self._rows(
            TaskQuery.RENEW,
            (
                lease.state.task_id,
                lease.worker_id,
                lease.token,
                lease_sec,
            ),
        )
        if not rows:
            raise TaskServiceError(
                TaskCode.LEASE_LOST,
                f"Task {lease.state.task_id}: renewal rejected "
                f"for worker {lease.worker_id}",
            )

        return rows[0].lease()

    async def progress(self, lease: TaskLease, value: JsonValue) -> TaskState:
        rows = await self._rows(
            TaskQuery.PROGRESS,
            (
                lease.state.task_id,
                lease.worker_id,
                lease.token,
                Jsonb(value),
            ),
        )
        if not rows:
            raise TaskServiceError(
                TaskCode.LEASE_LOST,
                f"Task {lease.state.task_id}: progress rejected "
                f"for worker {lease.worker_id}",
            )

        return rows[0].state

    async def finish(self, lease: TaskLease, outcome: TaskOutcome) -> TaskState:
        result = None
        if outcome.result is not None:
            result = Jsonb(outcome.result)

        error = None
        if outcome.error is not None:
            error = Jsonb(outcome.error.model_dump(mode="json"))

        rows = await self._rows(
            TaskQuery.FINISH,
            (
                lease.state.task_id,
                lease.worker_id,
                lease.token,
                outcome.status.value,
                result,
                error,
            ),
        )
        if rows:
            return rows[0].state

        rows = await self._rows(TaskQuery.RECOVER, (lease.worker_id, lease.token))
        if rows:
            state = rows[0].state
            if state.task_id != lease.state.task_id:
                raise TaskServiceError(
                    TaskCode.LEASE_LOST, "Task outcome references a different lease"
                )

            if state.status.terminal:
                saved = TaskOutcome(
                    status=state.status, result=state.result, error=state.error
                )
                if saved == outcome:
                    return state

        raise TaskServiceError(
            TaskCode.LEASE_LOST,
            f"Task {lease.state.task_id}: outcome rejected "
            f"for worker {lease.worker_id}",
        )

    async def expire(self, limit: int = 100) -> tuple[TaskState, ...]:
        if limit <= 0:
            raise TaskServiceError(
                TaskCode.INVALID, "Task expiry scan requires a positive batch size"
            )

        rows = await self._rows(TaskQuery.EXPIRE, (limit,))
        states = self._states(rows)
        return tuple(states)

    @staticmethod
    def _states(rows: Sequence[TaskRow]) -> Iterator[TaskState]:
        for row in rows:
            yield row.state

    @staticmethod
    def _duration(seconds: float) -> None:
        if not isfinite(seconds):
            raise TaskServiceError(
                TaskCode.INVALID, "Task lease duration must be finite"
            )

        if seconds <= 0:
            raise TaskServiceError(
                TaskCode.INVALID, "Task lease duration must be positive"
            )
