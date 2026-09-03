"""Хранимые сущности workflow: определение и запуск, как их держит хранилище.

Ошибки:
WorkflowStoreError — хранилище недоступно или отказало.
WorkflowNotFoundError — у владельца нет определения или запуска с таким id.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, computed_field

from boba.identity.errors import RefusalError
from boba.toolkit.result import ToolResult
from boba.workflow.graph import RunState, RunStatus, TaskStatus

__all__ = [
    "RunOutcome",
    "RunsColumn",
    "StopOutcome",
    "StoredRun",
    "StoredWorkflow",
    "TaskOutcome",
    "WorkflowError",
    "WorkflowNotFoundError",
    "WorkflowRefusal",
    "WorkflowRunError",
    "WorkflowStoreError",
    "WorkflowTable",
    "WorkflowsColumn",
]


class WorkflowTable(StrEnum):
    """Таблицы workflow: определения и запуски; черновик — поля строки workflows."""

    WORKFLOWS = "workflows"
    RUNS = "workflow_runs"


class WorkflowsColumn(StrEnum):
    """Колонки workflows."""

    ID = "id"
    USER_ID = "user_id"
    NAME = "name"
    SPEC = "spec"
    TOOLS = "tools"
    LAYOUT = "layout"
    DRAFT_SPEC = "draft_spec"
    DRAFT_LAYOUT = "draft_layout"
    DRAFT_REVISION = "draft_revision"
    CREATED_AT = "created_at"
    UPDATED_AT = "updated_at"


class RunsColumn(StrEnum):
    """Колонки workflow_runs."""

    ID = "id"
    WORKFLOW_ID = "workflow_id"
    USER_ID = "user_id"
    INITIATOR = "initiator"
    PROFILE = "profile"
    STATUS = "status"
    STATE = "state"
    INSTANCE = "instance"
    STARTED_AT = "started_at"
    FINISHED_AT = "finished_at"


class WorkflowStoreError(Exception):
    """База отказала или строка не сохранилась."""


class WorkflowNotFoundError(WorkflowStoreError):
    """Строки нет либо она принадлежит другому пользователю."""


class WorkflowNameTakenError(WorkflowStoreError):
    """Имя занято другой строкой того же пользователя."""


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
    draft_spec: str | None = None
    """Несохранённые правки: YAML черновика; None — правок нет, строка чистая."""
    draft_layout: Mapping[str, Any] | None = None
    draft_revision: int = 0
    """Растёт с каждой записью черновика: вкладки применяют только новее своего."""
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
