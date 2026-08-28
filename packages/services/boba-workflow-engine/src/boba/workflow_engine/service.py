"""Единственный вход к workflow: сохранить, проверить, запустить, остановить.

Ошибки:
WorkflowError — спека негодна или запрещённые инструменты; workflow не
    найден у владельца; kind из WorkflowRefusal.
LockBusyError — область запуска занята живым держателем.
WorkflowStoreError — хранилище недоступно.
WorkflowRunError — раннер нарушил контракт инструментов.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence
from enum import StrEnum
from typing import Any, ClassVar
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict

from boba.cancellation import StopReason
from boba.identity.context import CallContext, Scope, Subject
from boba.identity.errors import RefusalError
from boba.identity.locks import (
    LiveLock,
    LiveLocks,
    LockKeeper,
    LockLostError,
    LockMode,
    LockPurpose,
    LockToken,
    RunLocking,
)
from boba.identity.run import BackgroundRuns, RunRegistry
from boba.messaging import MessageBus, RunFinished, RunStateChanged, StopRequested
from boba.toolkit.result import ToolResult
from boba.toolrun.invoke import ToolInvoker
from boba.toolrun.registry import ToolRegistry
from boba.workflow import (
    RunState,
    ToolCatalog,
    WorkflowGraph,
    WorkflowPlan,
    WorkflowSpec,
    WorkflowSpecError,
)
from boba.workflow.events import RunSnapshot
from boba.workflow.ports import RunSink
from boba.workflow.records import StoredRun, StoredWorkflow, WorkflowNotFoundError
from boba.workflow_engine.catalog import CatalogBuilder
from boba.workflow_engine.runner import WorkflowRunner
from boba.workflow_engine.store import WorkflowStore

__all__ = [
    "RunOutcome",
    "StartedRun",
    "StopOutcome",
    "WorkflowError",
    "WorkflowRefusal",
    "WorkflowService",
]

logger = logging.getLogger(__name__)

RegistrySource = Callable[[], Awaitable["ToolRegistry"]]


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


class StartedRun(BaseModel):
    """Записанный, но ещё не исполненный запуск: запись хранилища, граф и вызыватель
    инструментов.
    """

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    record: StoredRun
    graph: WorkflowGraph
    invoker: ToolInvoker
    lock: LiveLock


class _StoreSink(RunSink):
    """Принимает снимки запуска от раннера: под живой блокировкой пишет снимок в
    запись запуска и сообщает о нём в шину указателем RunStateChanged, а при
    терминальном статусе — RunFinished.
    """

    def __init__(
        self,
        store: WorkflowStore,
        bus: MessageBus,
        locks: LiveLocks,
        run_id: UUID,
        token: LockToken,
    ) -> None:
        self._store = store
        self._bus = bus
        self._locks = locks
        self._run_id = run_id
        self._token = token
        self._scope = Scope.workflow(run_id)

    async def snapshot(self, state: RunState) -> None:
        # снимок пишет только живой держатель: зомби не перезапишет чужую работу
        if not await self._locks.heartbeat(self._token):
            msg = f"lock of {self._scope.render()} is lost: snapshot refused"
            raise LockLostError(msg)

        await self._store.update_run(self._run_id, state)

        status = state.status.value
        changed = RunStateChanged(run_id=self._run_id, status=status)
        await self._bus.publish(self._scope, changed, self._token)

        if not state.status.terminal:
            return

        finished = RunFinished(run_id=self._run_id, status=status)
        await self._bus.publish(self._scope, finished, self._token)


class WorkflowService:
    """Единственный вход к workflow под субъектом: сохранить, проверить, запустить,
    остановить.
    """

    def __init__(
        self,
        store: WorkflowStore,
        registry: RegistrySource,
        instance: str,
        bus: MessageBus,
        locking: RunLocking,
    ) -> None:
        self._store = store
        self._registry = registry
        self._instance = instance
        self._bus = bus
        self._locks = locking.locks
        self._heartbeat_sec = locking.heartbeat_sec
        self._background = BackgroundRuns()

    @property
    def locks(self) -> LiveLocks:
        """Блокировки областей запусков."""
        return self._locks

    @property
    def bus(self) -> MessageBus:
        """Шина сообщений процесса; получатели подписываются на область запуска."""
        return self._bus

    async def snapshot_of(self, run_id: UUID) -> RunSnapshot:
        """Возвращает текущий снимок запуска по id без проверки владения — для
        получателей шины, которые прошли проверку при подписке.
        """
        record = await self._store.run_by_id(run_id)
        return RunSnapshot(run_id=record.id, status=record.status, state=record.state)

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
        """Снимок до старта — спека, стадии, задачи в статусе pending — чтобы страница
        нарисовала граф сразу.
        """
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
        """Проверка и запись запуска."""
        graph = await self.validate(context.subject, stored.spec)
        registry = await self._registry()
        subject = context.subject
        invoker = ToolInvoker(registry.for_headless(subject.roles, subject.profile))
        initial = self.initial_state(graph)

        lock = await self._locks.acquire(
            Scope.workflow(run_id), LockMode.EXCLUSIVE, LockPurpose.RUN, subject.user_id
        )
        try:
            record = await self._store.start_run(
                run_id,
                stored.id,
                context.subject.user_id,
                context.initiator.model_dump(mode="json"),
                context.subject.profile,
                initial,
                self._instance,
            )
        except Exception:
            await self._locks.release(lock.token)
            raise
        logger.info(
            "workflow %s run %s started by %s (%s)",
            stored.name,
            run_id,
            context.subject.login,
            context.initiator.kind,
        )

        return StartedRun(record=record, graph=graph, invoker=invoker, lock=lock)

    async def execute(self, context: CallContext, started: StartedRun) -> RunOutcome:
        """Исполнение записанного запуска; Stop вызывающего останавливает и его."""
        run_id = started.record.id
        run_context = context.in_scope(Scope.workflow(run_id))
        runner = WorkflowRunner(started.invoker, WorkflowRunner.utc_now)
        sink = _StoreSink(
            self._store, self._bus, self._locks, run_id, started.lock.token
        )
        keeper = LockKeeper(
            self._locks, started.lock, run_context.cancellation, self._heartbeat_sec
        )

        async with keeper:
            with context.cancellation.abort_with(run_context.cancellation.cancel):
                state, results = await runner.run(started.graph, run_context, sink)

        logger.info("workflow run %s finished: %s", run_id, state.status)
        record = await self._store.get_run(context.subject.user_id, run_id)
        return RunOutcome(run=record, state=state, results=results)

    def launch(self, context: CallContext, started: StartedRun) -> None:
        """Исполняет записанный запуск в фоне процесса: страница и планировщик итога не
        ждут.
        """
        name = f"workflow-run:{started.record.id}"
        self._background.launch(name, self.execute(context, started))

    ABANDONED: ClassVar[str] = "the process running this workflow was restarted"

    async def stop(self, subject: Subject, run_id: UUID) -> StopOutcome:
        """Останавливает запуск владельца: свой — через реестр или как сироту, чужой
        без держателя — как сироту, чужой живой — командой в шину (ACCEPTED);
        завершённый даёт FINISHED.
        """
        try:
            record = await self._store.get_run(subject.user_id, run_id)
        except WorkflowNotFoundError:
            return StopOutcome.FINISHED

        if record.state.status.terminal:
            return StopOutcome.FINISHED

        if record.instance == self._instance:
            if RunRegistry.stop(str(run_id), StopReason.USER_STOP):
                return StopOutcome.STOPPED

            await self._abandon(record)
            return StopOutcome.STOPPED

        scope = Scope.workflow(run_id)
        holders = await self._locks.holders_of(scope)
        if not holders:
            await self._abandon(record)
            return StopOutcome.STOPPED

        command = StopRequested(by_user=subject.user_id, by_instance=self._instance)
        command_id = await self._bus.command(scope, command)
        logger.info(
            "workflow run %s: stop requested from %s as command %d",
            run_id,
            self._instance,
            command_id,
        )
        return StopOutcome.ACCEPTED

    async def recover_orphans(self) -> int:
        """Закрывает как failed запуски этого инстанса, оставшиеся без процесса после
        перезапуска.
        """
        orphans = await self._store.orphans_of(self._instance)
        for record in orphans:
            await self._abandon(record)
            logger.warning("workflow run %s abandoned: %s", record.id, self.ABANDONED)

        return len(orphans)

    async def close_unlocked(self) -> int:
        """Закрывает как failed незавершённые запуски, у которых нет живой
        блокировки, и возвращает их число.
        """
        closed = 0
        for record in await self._store.running():
            holders = await self._locks.holders_of(Scope.workflow(record.id))
            if holders:
                continue

            await self._abandon(record)
            closed += 1
            logger.warning("workflow run %s abandoned: no live holder", record.id)

        return closed

    async def _abandon(self, record: StoredRun) -> None:
        """Закрывает запуск как failed под своей блокировкой уборки, чтобы снимок
        прошёл fencing шины.
        """
        scope = Scope.workflow(record.id)
        lock = await self._locks.acquire(
            scope, LockMode.EXCLUSIVE, LockPurpose.CLEANUP, record.user_id
        )
        try:
            state = record.state.abandoned(self.ABANDONED, WorkflowRunner.utc_now())
            sink = _StoreSink(
                self._store, self._bus, self._locks, record.id, lock.token
            )
            await sink.snapshot(state)
        finally:
            await self._locks.release(lock.token)
