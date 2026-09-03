"""Порты движка workflow: хранилище определений и запусков, приёмник снимков."""

from __future__ import annotations

from abc import abstractmethod
from collections.abc import Mapping, Sequence
from typing import Any, Protocol
from uuid import UUID

from boba.messaging import StreamFeed
from boba.workflow.graph import RunState
from boba.workflow.records import StoredRun, StoredWorkflow
from boba.workflow.spec import WorkflowSpec

__all__ = ["RunSink", "WorkflowRepository"]


class RunSink(StreamFeed, Protocol):
    """Куда уходят снимки состояния и рост журналов вызовов по ходу запуска."""

    @abstractmethod
    async def snapshot(self, state: RunState) -> None: ...


class WorkflowRepository(Protocol):
    """Хранилище определений и запусков; реализация — WorkflowStore на postgres."""

    @abstractmethod
    async def setup(self) -> None: ...

    @abstractmethod
    async def save(
        self, user_id: UUID, spec: WorkflowSpec, layout: Mapping[str, Any]
    ) -> StoredWorkflow: ...

    @abstractmethod
    async def save_into(
        self,
        user_id: UUID,
        workflow_id: UUID,
        spec: WorkflowSpec,
        layout: Mapping[str, Any],
    ) -> StoredWorkflow:
        """Переписывает строку по id: имя, спека, раскладка; черновик снимается."""
        ...

    @abstractmethod
    async def get(self, user_id: UUID, workflow_id: UUID) -> StoredWorkflow: ...

    @abstractmethod
    async def get_by_name(self, user_id: UUID, name: str) -> StoredWorkflow: ...

    @abstractmethod
    async def list_for(self, user_id: UUID) -> Sequence[StoredWorkflow]: ...

    @abstractmethod
    async def delete(self, user_id: UUID, workflow_id: UUID) -> bool: ...

    @abstractmethod
    async def put_draft(
        self, user_id: UUID, workflow_id: UUID, spec: str, layout: Mapping[str, Any]
    ) -> StoredWorkflow:
        """Пишет черновик в строку workflow; draft_revision растёт на единицу."""
        ...

    @abstractmethod
    async def clear_draft(self, user_id: UUID, workflow_id: UUID) -> StoredWorkflow:
        """Сбрасывает черновик: строка возвращается к сохранённому состоянию."""
        ...

    @abstractmethod
    async def start_run(  # noqa: PLR0913 — запуск описывается всеми полями сразу
        self,
        run_id: UUID,
        workflow_id: UUID | None,
        user_id: UUID,
        initiator: Mapping[str, Any],
        profile: str,
        state: RunState,
        instance: str,
    ) -> StoredRun: ...

    @abstractmethod
    async def update_run(self, run_id: UUID, state: RunState) -> None: ...

    @abstractmethod
    async def get_run(self, user_id: UUID, run_id: UUID) -> StoredRun: ...

    @abstractmethod
    async def list_runs(self, user_id: UUID, limit: int) -> Sequence[StoredRun]: ...
