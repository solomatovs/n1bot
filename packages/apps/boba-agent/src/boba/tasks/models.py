"""Контракты задач и исполнителей.

Ошибки:
TaskServiceError — операция хранилища не завершилась или нарушает контракт.
TaskExecutionError — исполнитель завершил действие с определённой ошибкой.
"""

from __future__ import annotations

from abc import abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Annotated, Protocol, Self
from uuid import UUID

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    JsonValue,
    PositiveFloat,
    PositiveInt,
    StringConstraints,
    model_validator,
)

Name = Annotated[str, StringConstraints(min_length=1)]


class TaskStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"

    @property
    def terminal(self) -> bool:
        return self not in (TaskStatus.PENDING, TaskStatus.RUNNING)


class TaskCode(StrEnum):
    STORAGE = "storage_unavailable"
    CONFLICT = "task_conflict"
    NOT_FOUND = "task_not_found"
    LEASE_LOST = "lease_lost"
    EXECUTION = "execution_failed"
    INTERRUPTED = "execution_interrupted"
    LISTENER = "listener_failed"
    INVALID = "invalid_task_operation"


class TaskModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class TaskError(TaskModel):
    code: Name
    message: Name
    details: dict[str, JsonValue] = {}


class TaskCreate(TaskModel):
    """Команда добавления; одинаковый task_id с одинаковыми данными идемпотентен."""

    task_id: Name
    kind: Name
    route_key: Name | None = None
    payload: dict[str, JsonValue]
    run_timeout_sec: PositiveFloat


class TaskOutcome(TaskModel):
    status: TaskStatus
    result: JsonValue = None
    error: TaskError | None = None

    @model_validator(mode="after")
    def terminal_outcome(self) -> Self:
        if not self.status.terminal:
            raise ValueError("A task outcome must be terminal")

        requires_error = self.status in (TaskStatus.FAILED, TaskStatus.INTERRUPTED)
        if requires_error != (self.error is not None):
            raise ValueError("Failed and interrupted outcomes require an error")

        if self.status is not TaskStatus.COMPLETED:
            if self.result is not None:
                raise ValueError("Only a completed task can have a final result")

        return self


class TaskState(TaskCreate):
    """Сохранённое состояние для производителя; токен владения сюда не входит."""

    owner_id: Name
    status: TaskStatus
    revision: PositiveInt
    created_at: AwareDatetime
    started_at: AwareDatetime | None
    finished_at: AwareDatetime | None
    execution_deadline_at: AwareDatetime | None
    progress: JsonValue = None
    result: JsonValue = None
    error: TaskError | None = None


class TaskChanged(TaskModel):
    task_id: Name
    revision: PositiveInt


class WorkerConfig(TaskModel):
    concurrency: PositiveInt = 8
    lease_sec: PositiveFloat = 60
    heartbeat_sec: PositiveFloat = 15
    poll_sec: PositiveFloat = 1
    maintenance_sec: PositiveFloat = 5
    maintenance_batch: PositiveInt = 100
    shutdown_sec: PositiveFloat = 5
    retry_sec: PositiveFloat = 1

    @model_validator(mode="after")
    def heartbeat_precedes_expiry(self) -> Self:
        if self.heartbeat_sec >= self.lease_sec:
            raise ValueError("heartbeat_sec must be less than lease_sec")

        return self


@dataclass(frozen=True)
class WorkerIdentity:
    """Полномочия worker задаёт сборка сервиса, независимо от входов пользователя."""

    worker_id: str
    kinds: frozenset[str]
    routes: frozenset[str] = frozenset()
    owners: frozenset[str] = frozenset()
    all_owners: bool = False

    def __post_init__(self) -> None:
        if not self.worker_id:
            raise ValueError("worker_id must not be empty")

    def permits(self, state: TaskState) -> bool:
        if state.kind not in self.kinds:
            return False

        if state.route_key is not None:
            if state.route_key not in self.routes:
                return False

        if self.all_owners:
            return True

        return state.owner_id in self.owners


@dataclass(frozen=True)
class TaskLease:
    state: TaskState
    worker_id: str
    token: UUID
    expires_at: AwareDatetime
    remaining_sec: float = field(compare=False)


class TaskServiceError(RuntimeError):
    def __init__(self, code: TaskCode, message: str) -> None:
        self.code = code
        super().__init__(message)


class TaskExecutionError(RuntimeError):
    """Определённый отказ действия; неизвестный исход требует interrupted."""

    def __init__(self, error: TaskError) -> None:
        self.error = error
        super().__init__(error.message)


@dataclass(frozen=True)
class ExecutionContext:
    """Исполнитель сообщает прогресс через worker, который проверяет владение."""

    report_progress: Callable[[JsonValue], Awaitable[None]]


class TaskExecutor(Protocol):
    """Выполняет сохранённое действие; жизненным циклом в таблице управляет worker."""

    @abstractmethod
    async def execute(
        self, task: TaskState, context: ExecutionContext
    ) -> TaskOutcome: ...
