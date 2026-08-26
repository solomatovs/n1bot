"""Единственный вход к workflow: сохранить, проверить, запустить, остановить.

Кто бы ни триггерил — LLM инструментом, человек со страницы, планировщик —
запуск идёт здесь: спека проверяется против каталога инструментов субъекта,
запись о запуске ложится в хранилище, раннер исполняет граф под контекстом
`scope = workflow/run_id`. Остановка каждого запуска этого инстанса — по id.

Ошибки:
WorkflowError — спека негодна или запрещённые инструменты; workflow не
    найден у владельца; kind из WorkflowRefusal.
WorkflowStoreError — хранилище недоступно.
WorkflowRunError — раннер нарушил контракт инструментов.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence
from enum import StrEnum
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict

from boba.cancellation import StopReason
from boba.chainlit.agent.invoke import ToolInvoker
from boba.chainlit.domain.context import CallContext, Scope, Subject
from boba.chainlit.domain.errors import RefusalError
from boba.chainlit.domain.run import BackgroundRuns, RunRegistry
from boba.chainlit.workflow.catalog import CatalogBuilder
from boba.chainlit.workflow.events import RunEvents, RunSnapshot
from boba.chainlit.workflow.runner import RunSink, WorkflowRunner
from boba.chainlit.workflow.store import (
    StoredRun,
    StoredWorkflow,
    WorkflowNotFoundError,
    WorkflowStore,
)
from boba.toolkit.result import ToolResult
from boba.workflow import (
    RunState,
    ToolCatalog,
    WorkflowGraph,
    WorkflowPlan,
    WorkflowSpec,
    WorkflowSpecError,
)

if TYPE_CHECKING:
    from boba.chainlit.infra.plugins import ToolRegistry

__all__ = [
    "RunOutcome",
    "StartedRun",
    "WorkflowError",
    "WorkflowRefusal",
    "WorkflowService",
]

logger = logging.getLogger(__name__)

RegistrySource = Callable[[], Awaitable["ToolRegistry"]]


class WorkflowRefusal(StrEnum):
    """Отказы сервиса workflow."""

    BAD_SPEC = "bad_workflow_spec"
    NOT_FOUND = "workflow_not_found"


class WorkflowError(RefusalError):
    """Workflow отклонён; текст причины готов для LLM и страницы."""


class RunOutcome(BaseModel):
    """Итог запуска: запись хранилища и результаты задач по именам."""

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    run: StoredRun
    state: RunState
    results: Mapping[str, ToolResult]


class StartedRun(BaseModel):
    """Записанный, но ещё не исполненный запуск: что и чем гнать."""

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    record: StoredRun
    graph: WorkflowGraph
    invoker: ToolInvoker


class _StoreSink(RunSink):
    """Снимок сначала в запись запуска, затем слушателям."""

    def __init__(self, store: WorkflowStore, events: RunEvents, run_id: UUID) -> None:
        self._store = store
        self._events = events
        self._run_id = run_id

    async def snapshot(self, state: RunState) -> None:
        await self._store.update_run(self._run_id, state)
        await self._events.publish(
            RunSnapshot(run_id=self._run_id, status=state.status, state=state)
        )


class WorkflowService:
    """Сохранение, проверка, запуск и остановка workflow под субъектом."""

    def __init__(
        self,
        store: WorkflowStore,
        registry: RegistrySource,
        instance: str,
        events: RunEvents,
    ) -> None:
        self._store = store
        self._registry = registry
        self._instance = instance
        self._events = events
        self._background = BackgroundRuns()

    @property
    def events(self) -> RunEvents:
        """Шина снимков: сокет страницы подписывается на неё."""
        return self._events

    @property
    def instance(self) -> str:
        return self._instance

    async def catalog(self, subject: Subject) -> ToolCatalog:
        registry = await self._registry()
        return CatalogBuilder.of(registry, subject.roles, subject.profile)

    async def validate(self, subject: Subject, spec_text: str) -> WorkflowGraph:
        """Разбор и проверка спеки против каталога субъекта; отказ — WorkflowError."""
        catalog = await self.catalog(subject)

        try:
            spec = WorkflowSpec.parse_yaml(spec_text)
            return WorkflowGraph.build(spec, catalog)
        except WorkflowSpecError as exc:
            raise WorkflowError(WorkflowRefusal.BAD_SPEC, str(exc)) from exc

    async def save(
        self, subject: Subject, spec_text: str, layout: Mapping[str, Any]
    ) -> StoredWorkflow:
        graph = await self.validate(subject, spec_text)

        return await self._store.save(subject.user_id, graph.spec, layout)

    async def list_workflows(self, subject: Subject) -> Sequence[StoredWorkflow]:
        return await self._store.list_for(subject.user_id)

    async def get(self, subject: Subject, workflow_id: int) -> StoredWorkflow:
        try:
            return await self._store.get(subject.user_id, workflow_id)
        except WorkflowNotFoundError as exc:
            raise WorkflowError(WorkflowRefusal.NOT_FOUND, str(exc)) from exc

    async def get_by_name(self, subject: Subject, name: str) -> StoredWorkflow:
        try:
            return await self._store.get_by_name(subject.user_id, name)
        except WorkflowNotFoundError as exc:
            raise WorkflowError(WorkflowRefusal.NOT_FOUND, str(exc)) from exc

    async def delete(self, subject: Subject, workflow_id: int) -> bool:
        return await self._store.delete(subject.user_id, workflow_id)

    async def get_run(self, subject: Subject, run_id: UUID) -> StoredRun:
        try:
            return await self._store.get_run(subject.user_id, run_id)
        except WorkflowNotFoundError as exc:
            raise WorkflowError(WorkflowRefusal.NOT_FOUND, str(exc)) from exc

    async def list_runs(self, subject: Subject, limit: int) -> Sequence[StoredRun]:
        return await self._store.list_runs(subject.user_id, limit)

    @staticmethod
    def initial_state(graph: WorkflowGraph) -> RunState:
        """Снимок до старта: спека, стадии, задачи pending — для страницы."""
        return WorkflowPlan(graph).snapshot()

    @staticmethod
    def new_run_id() -> UUID:
        return uuid4()

    async def run(
        self, context: CallContext, stored: StoredWorkflow, run_id: UUID
    ) -> RunOutcome:
        """Запуск сохранённого workflow и ожидание итога."""
        started = await self.start(context, stored, run_id)

        return await self.execute(context, started)

    async def start(
        self, context: CallContext, stored: StoredWorkflow, run_id: UUID
    ) -> StartedRun:
        """Проверка и запись запуска.

        Спека проверяется заново: гранты могли смениться со времени сохранения.
        """
        graph = await self.validate(context.subject, stored.spec)
        registry = await self._registry()
        subject = context.subject
        invoker = ToolInvoker(registry.for_headless(subject.roles, subject.profile))
        initial = self.initial_state(graph)

        record = await self._store.start_run(
            run_id,
            stored.id,
            context.subject.user_id,
            context.initiator.model_dump(mode="json"),
            context.subject.profile,
            initial,
            self._instance,
        )
        logger.info(
            "workflow %s run %s started by %s (%s)",
            stored.name,
            run_id,
            context.subject.login,
            context.initiator.kind,
        )

        return StartedRun(record=record, graph=graph, invoker=invoker)

    async def execute(self, context: CallContext, started: StartedRun) -> RunOutcome:
        """Исполнение записанного запуска; Stop вызывающего останавливает и его."""
        run_id = started.record.id
        run_context = context.in_scope(Scope.workflow(run_id))
        runner = WorkflowRunner(started.invoker, WorkflowRunner.utc_now)
        sink = _StoreSink(self._store, self._events, run_id)

        with context.cancellation.abort_with(run_context.cancellation.cancel):
            state, results = await runner.run(started.graph, run_context, sink)

        logger.info("workflow run %s finished: %s", run_id, state.status)
        record = await self._store.get_run(context.subject.user_id, run_id)
        return RunOutcome(run=record, state=state, results=results)

    def launch(self, context: CallContext, started: StartedRun) -> None:
        """Исполнение в фоне процесса: страница и планировщик итога не ждут."""
        name = f"workflow-run:{started.record.id}"
        self._background.launch(name, self.execute(context, started))

    async def stop(self, subject: Subject, run_id: UUID) -> bool:
        """Останавливает живой запуск владельца на этом инстансе; False — нечего."""
        try:
            await self._store.get_run(subject.user_id, run_id)
        except WorkflowNotFoundError:
            return False

        return RunRegistry.stop(str(run_id), StopReason.USER_STOP)
