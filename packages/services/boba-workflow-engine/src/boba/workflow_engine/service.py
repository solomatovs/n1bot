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
from dataclasses import dataclass
from typing import Any, ClassVar
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict

from boba.cancellation import StopReason
from boba.identity.context import CallContext, Scope, Subject
from boba.identity.locks import (
    LiveLock,
    LiveLocks,
    LockKeeper,
    LockLostError,
    LockMode,
    LockPurpose,
    RunLocking,
)
from boba.identity.run import BackgroundRuns, RunRegistry
from boba.messaging import (
    ChangeAction,
    LockToken,
    MessageBus,
    RunFinished,
    RunListChanged,
    RunStateChanged,
    StopRequested,
    StreamAppended,
    WorkflowChanged,
    WorkflowDraftChanged,
)
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
from boba.workflow.records import (
    DraftKey,
    RunOutcome,
    StopOutcome,
    StoredRun,
    StoredWorkflow,
    WorkflowDraft,
    WorkflowError,
    WorkflowNotFoundError,
    WorkflowRefusal,
)
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


class StartedRun(BaseModel):
    """Записанный, но ещё не исполненный запуск: запись хранилища, граф и вызыватель
    инструментов.
    """

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    record: StoredRun
    graph: WorkflowGraph
    invoker: ToolInvoker
    lock: LiveLock
    workflow_name: str


@dataclass(frozen=True)
class SinkTarget:
    """Чей снимок принимает приёмник: запись запуска, token держателя и имя workflow
    для ленты пользователя.
    """

    record: StoredRun
    token: LockToken
    workflow_name: str


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
        target: SinkTarget,
    ) -> None:
        record = target.record
        self._store = store
        self._bus = bus
        self._locks = locks
        self._run_id = record.id
        self._token = target.token
        self._scope = Scope.workflow(record.id)
        self._listing = RunListChanged(
            run_id=record.id,
            workflow_id=record.workflow_id,
            workflow_name=target.workflow_name,
            status=record.status.value,
        )
        self._user_scope = Scope.user(record.user_id)

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

        listing = self._listing.model_copy(update={"status": status})
        await self._bus.publish(self._user_scope, listing, LockToken.local())

    async def stream_appended(self, message: StreamAppended) -> None:
        await self._bus.publish(self._scope, message, self._token)


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

        saved = await self._store.save(subject.user_id, graph.spec, layout)
        changed = WorkflowChanged(
            workflow_id=saved.id, name=saved.name, action=ChangeAction.UPDATED
        )
        await self._bus.publish(Scope.user(subject.user_id), changed, LockToken.local())

        # сохранённое становится истиной: черновик вкладок этого workflow снимается
        await self.drop_draft(subject, DraftKey.of_workflow(saved.id), "")

        return saved

    async def put_draft(
        self,
        subject: Subject,
        key: DraftKey,
        spec: str,
        layout: Mapping[str, Any],
        by_sid: str,
    ) -> WorkflowDraft:
        """Пишет общий черновик билдера без проверки спеки — она правится по ходу — и
        сообщает вкладкам пользователя новую revision.
        """
        draft = await self._store.put_draft(subject.user_id, key, spec, layout)
        await self._draft_changed(
            subject.user_id, key, draft.revision, by_sid, ChangeAction.UPDATED
        )

        return draft

    async def get_draft(self, subject: Subject, key: DraftKey) -> WorkflowDraft:
        try:
            return await self._store.get_draft(subject.user_id, key)
        except WorkflowNotFoundError as exc:
            raise WorkflowError(WorkflowRefusal.NOT_FOUND, str(exc)) from exc

    async def list_drafts(self, subject: Subject) -> Sequence[WorkflowDraft]:
        """Черновики пользователя для списка workflow, свежие сверху."""
        return await self._store.list_drafts(subject.user_id)

    async def drop_draft(self, subject: Subject, key: DraftKey, by_sid: str) -> bool:
        dropped = await self._store.drop_draft(subject.user_id, key)
        if not dropped:
            return False

        await self._draft_changed(subject.user_id, key, 0, by_sid, ChangeAction.DELETED)
        return True

    async def _draft_changed(
        self,
        user_id: UUID,
        key: DraftKey,
        revision: int,
        by_sid: str,
        action: ChangeAction,
    ) -> None:
        message = WorkflowDraftChanged(
            key=key.render(), revision=revision, by_sid=by_sid, action=action
        )
        await self._bus.publish(Scope.user(user_id), message, LockToken.local())

    async def list_workflows(self, subject: Subject) -> Sequence[StoredWorkflow]:
        return await self._store.list_for(subject.user_id)

    async def get(self, subject: Subject, workflow_id: UUID) -> StoredWorkflow:
        try:
            return await self._store.get(subject.user_id, workflow_id)
        except WorkflowNotFoundError as exc:
            raise WorkflowError(WorkflowRefusal.NOT_FOUND, str(exc)) from exc

    async def get_by_name(self, subject: Subject, name: str) -> StoredWorkflow:
        try:
            return await self._store.get_by_name(subject.user_id, name)
        except WorkflowNotFoundError as exc:
            raise WorkflowError(WorkflowRefusal.NOT_FOUND, str(exc)) from exc

    async def delete(self, subject: Subject, workflow_id: UUID) -> bool:
        try:
            stored = await self._store.get(subject.user_id, workflow_id)
        except WorkflowNotFoundError:
            return False

        deleted = await self._store.delete(subject.user_id, workflow_id)
        if not deleted:
            return False

        changed = WorkflowChanged(
            workflow_id=workflow_id, name=stored.name, action=ChangeAction.DELETED
        )
        await self._bus.publish(Scope.user(subject.user_id), changed, LockToken.local())

        return True

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

        listing = RunListChanged(
            run_id=record.id,
            workflow_id=record.workflow_id,
            workflow_name=stored.name,
            status=record.status.value,
        )
        await self._bus.publish(Scope.user(subject.user_id), listing, LockToken.local())

        return StartedRun(
            record=record,
            graph=graph,
            invoker=invoker,
            lock=lock,
            workflow_name=stored.name,
        )

    async def execute(self, context: CallContext, started: StartedRun) -> RunOutcome:
        """Исполнение записанного запуска; Stop вызывающего останавливает и его."""
        run_id = started.record.id
        run_context = context.in_scope(Scope.workflow(run_id))
        runner = WorkflowRunner(started.invoker, WorkflowRunner.utc_now)
        target = SinkTarget(
            record=started.record,
            token=started.lock.token,
            workflow_name=started.workflow_name,
        )
        sink = _StoreSink(self._store, self._bus, self._locks, target)
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
            target = SinkTarget(record=record, token=lock.token, workflow_name="")
            sink = _StoreSink(self._store, self._bus, self._locks, target)
            await sink.snapshot(state)
        finally:
            await self._locks.release(lock.token)
