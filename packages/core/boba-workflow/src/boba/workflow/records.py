"""Хранимые сущности workflow: определение и запуск, как их держит хранилище.

Ошибки:
WorkflowStoreError — хранилище недоступно или отказало.
WorkflowNotFoundError — у владельца нет определения или запуска с таким id.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from enum import StrEnum
from typing import Any, ClassVar
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, computed_field

from boba.identity.errors import RefusalError
from boba.toolkit.result import ToolResult
from boba.workflow.graph import RunState, RunStatus, TaskStatus

__all__ = [
    "DraftKey",
    "DraftKind",
    "RunOutcome",
    "StopOutcome",
    "StoredRun",
    "StoredWorkflow",
    "TaskOutcome",
    "WorkflowDraft",
    "WorkflowError",
    "WorkflowNotFoundError",
    "WorkflowRefusal",
    "WorkflowRunError",
    "WorkflowStoreError",
]


class WorkflowStoreError(Exception):
    """Хранилище workflow недоступно или отказало."""


class WorkflowNotFoundError(WorkflowStoreError):
    """У владельца нет определения или запуска с таким id."""


class DraftKind(StrEnum):
    """Чей черновик: сохранённого workflow по id либо нового по uuid вкладки."""

    WORKFLOW = "workflow"
    NEW = "new"


class DraftKey(BaseModel):
    """Ключ черновика билдера: вид и идентификатор; сборка и разбор строки в одном
    месте.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    SEP: ClassVar[str] = ":"

    kind: DraftKind
    ident: str = Field(min_length=1, max_length=64, pattern=r"^[0-9a-f-]+$")

    def render(self) -> str:
        return f"{self.kind.value}{self.SEP}{self.ident}"

    @classmethod
    def of_workflow(cls, workflow_id: UUID) -> DraftKey:
        return cls(kind=DraftKind.WORKFLOW, ident=str(workflow_id))

    @classmethod
    def parse(cls, raw: str) -> DraftKey:
        kind, sep, ident = raw.partition(cls.SEP)
        if not sep:
            msg = f"bad draft key: {raw!r}"
            raise ValueError(msg)

        try:
            return cls(kind=DraftKind(kind), ident=ident)
        except ValueError as exc:
            msg = f"bad draft key: {raw!r}"
            raise ValueError(msg) from exc


class WorkflowDraft(BaseModel):
    """Черновик билдера, общий для вкладок пользователя: последняя правка побеждает,
    revision растёт с каждой записью.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    key: str
    user_id: UUID
    revision: int
    spec: str
    layout: Mapping[str, Any]
    updated_at: datetime


class StoredWorkflow(BaseModel):
    """Определение workflow, каким его хранит таблица."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID
    user_id: UUID
    name: str
    spec: str
    """YAML как сохранили: исходник для LLM, редактора и diff."""
    tools: tuple[str, ...]
    """Инструменты спеки: поиск «кто использует pg_query» без разбора YAML."""
    layout: Mapping[str, Any]
    """Позиции узлов редактора; пусто — страница раскладывает сама."""
    created_at: datetime
    updated_at: datetime


class StoredRun(BaseModel):
    """Запуск workflow: снимок спеки, кто и от чьего имени, состояние."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID
    workflow_id: UUID | None
    """None — определение удалили; запуск и его логи остаются."""
    user_id: UUID
    initiator: Mapping[str, Any]
    profile: str
    state: RunState
    instance: str
    started_at: datetime
    finished_at: datetime | None

    @computed_field
    @property
    def status(self) -> RunStatus:
        """Статус — проекция состояния; в JSON уходит рядом с ним."""
        return self.state.status


class WorkflowRefusal(StrEnum):
    """Виды отказов сервиса workflow: негодная спека, запрещённые инструменты, не
    найдено.
    """

    BAD_SPEC = "bad_workflow_spec"
    NOT_FOUND = "workflow_not_found"


class WorkflowError(RefusalError):
    """Workflow отклонён; текст причины готов для показа модели и странице."""


class StopOutcome(StrEnum):
    """Итог просьбы остановить запуск: остановлен здесь, принят для другого инстанса
    или уже завершён.
    """

    STOPPED = "stopped"
    ACCEPTED = "accepted"
    FINISHED = "finished"


class RunOutcome(BaseModel):
    """Итог запуска: запись хранилища, состояние и результаты задач по именам."""

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    run: StoredRun
    state: RunState
    results: Mapping[str, ToolResult]


class WorkflowRunError(Exception):
    """Раннер не может продолжать: контракт инструментов или автомата нарушен."""


class TaskOutcome:
    """Итог задачи: статус для автомата и результат для рёбер и отчёта."""

    def __init__(self, status: TaskStatus, result: ToolResult, error: str) -> None:
        self.status = status
        self.result = result
        self.error = error
