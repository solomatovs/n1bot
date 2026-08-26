"""Хранимые сущности workflow: определение и запуск, как их держит хранилище.

Ошибки:
WorkflowStoreError — хранилище недоступно или отказало.
WorkflowNotFoundError — у владельца нет определения или запуска с таким id.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, computed_field

from boba.workflow.graph import RunState, RunStatus

__all__ = [
    "StoredRun",
    "StoredWorkflow",
    "WorkflowNotFoundError",
    "WorkflowStoreError",
]


class WorkflowStoreError(Exception):
    """Хранилище workflow недоступно или отказало."""


class WorkflowNotFoundError(WorkflowStoreError):
    """У владельца нет определения или запуска с таким id."""


class StoredWorkflow(BaseModel):
    """Определение workflow, каким его хранит таблица."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: int
    user_id: int
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
    workflow_id: int | None
    """None — определение удалили; запуск и его логи остаются."""
    user_id: int
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
