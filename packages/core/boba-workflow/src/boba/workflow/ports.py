"""Порты движка workflow: хранилище определений и запусков, приёмник снимков."""

from __future__ import annotations

from abc import abstractmethod
from collections.abc import Mapping, Sequence
from typing import Any, Protocol
from uuid import UUID

from boba.workflow.graph import RunState
from boba.workflow.records import StoredRun, StoredWorkflow
from boba.workflow.spec import WorkflowSpec

__all__ = ["RunSink", "WorkflowRepository"]


class RunSink(Protocol):
    """Куда уходят снимки состояния по ходу запуска."""

    @abstractmethod
    async def snapshot(self, state: RunState) -> None: ...


class WorkflowRepository(Protocol):
    """Хранилище определений и запусков; реализация — WorkflowStore на postgres."""

    @abstractmethod
    async def setup(self) -> None: ...

    @abstractmethod
    async def save(
        self, user_id: int, spec: WorkflowSpec, layout: Mapping[str, Any]
    ) -> StoredWorkflow: ...

    @abstractmethod
    async def get(self, user_id: int, workflow_id: int) -> StoredWorkflow: ...

    @abstractmethod
    async def get_by_name(self, user_id: int, name: str) -> StoredWorkflow: ...

    @abstractmethod
    async def list_for(self, user_id: int) -> Sequence[StoredWorkflow]: ...

    @abstractmethod
    async def delete(self, user_id: int, workflow_id: int) -> bool: ...

    @abstractmethod
    async def start_run(  # noqa: PLR0913 — запуск описывается всеми полями сразу
        self,
        run_id: UUID,
        workflow_id: int | None,
        user_id: int,
        initiator: Mapping[str, Any],
        profile: str,
        state: RunState,
        instance: str,
    ) -> StoredRun: ...

    @abstractmethod
    async def update_run(self, run_id: UUID, state: RunState) -> None: ...

    @abstractmethod
    async def get_run(self, user_id: int, run_id: UUID) -> StoredRun: ...

    @abstractmethod
    async def list_runs(self, user_id: int, limit: int) -> Sequence[StoredRun]: ...
