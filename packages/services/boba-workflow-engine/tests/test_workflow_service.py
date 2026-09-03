"""Сервис workflow на живом postgres: сохранение, запуск, рёбра, стоп.

Стенд: реестр инструментов-зондов с той же обвязкой, что ставит load_tools;
контекст хода чата — напрямую, без сессии chainlit.
"""

from __future__ import annotations

import asyncio
from uuid import UUID

import pytest
from psycopg import sql

from boba.cancellation import StopReason
from boba.db.postgres import AsyncPostgresPool
from boba.identity.context import CallContext, LlmInitiator, Scope, ScopeKind, Subject
from boba.identity.locks import MemoryLiveLocks, RunLocking
from boba.messaging import Envelope, MemoryMessageBus, WorkflowDraftChanged
from boba.runtime.commands import CommandRunner
from boba.stand.context import use_context
from boba.stand.tools import PROBE_ROLE as ROLE
from boba.stand.tools import Probe
from boba.toolkit.calls import ScriptCall, ToolCallViews
from boba.toolkit.result import (
    ErrorResult,
    MultiResult,
)
from boba.toolrun.registry import ToolRegistry
from boba.workflow import RunStatus, TaskStatus
from boba.workflow.report import ReportKey
from boba.workflow_engine.service import (
    StopOutcome,
    WorkflowError,
    WorkflowRefusal,
    WorkflowService,
)
from boba.workflow_engine.store import WorkflowConfig, WorkflowStore
from boba.workflow_engine.tools import (
    WorkflowToolConfig,
    build_workflow_tools,
)

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

SCHEMA = "workflow_service_test"
OWNER = UUID(int=7)
STRANGER = UUID(int=8)
THREAD = "wf-thread"


@pytest.fixture
async def store(pool: AsyncPostgresPool) -> WorkflowStore:
    async with pool.connection() as conn:
        await conn.execute(
            sql.SQL("drop schema if exists {} cascade").format(sql.Identifier(SCHEMA))
        )
    built = WorkflowStore(WorkflowConfig(enable=True, db_schema=SCHEMA), pool)
    await built.setup()
    return built


@pytest.fixture
def probe() -> Probe:
    return Probe()


@pytest.fixture
def service(store: WorkflowStore, probe: Probe) -> WorkflowService:
    async def registry() -> ToolRegistry:
        return probe.registry(["*"])

    return WorkflowService(
        store,
        registry,
        "test:0",
        MemoryMessageBus("test:0"),
        RunLocking(locks=MemoryLiveLocks("test:0", 20), heartbeat_sec=1.0),
    )


@pytest.fixture
def context(monkeypatch: pytest.MonkeyPatch) -> CallContext:
    return use_context(monkeypatch, thread_id=THREAD, user_id=OWNER, roles=[ROLE])


PARALLEL = """
name: parallel
tasks:
  a: {tool: slow, args: {label: a, delay: 0.4}}
  b: {tool: slow, args: {label: b, delay: 0.4}}
  c: {tool: slow, args: {label: c, delay: 0}}
edges:
  - "[a, b] -> c"
"""

VALUES = """
name: values
tasks:
  first: {tool: echo, args: {text: hello, intent: say hello}}
  second: {tool: echo, args: {text: "{{ first }} world"}}
  third: {tool: echo}
edges:
  - first.result -> second.args.text
  - second.result -> third.args.text
"""

FAILING = """
name: failing
tasks:
  boom: {tool: fail, args: {text: broken}}
  after: {tool: echo, args: {text: never}}
  aside: {tool: echo, args: {text: still}}
edges:
  - boom -> after
"""

LONG = """
name: long
tasks:
  wait: {tool: slow, args: {label: wait, delay: 30}}
  then: {tool: echo, args: {text: after}}
edges:
  - wait -> then
"""


class TestSave:
    async def test_unknown_tool_is_refused(
        self, service: WorkflowService, context: CallContext
    ) -> None:
        spec = "name: x\ntasks:\n  t: {tool: nope}\n"
        with pytest.raises(WorkflowError) as caught:
            await service.save(context.subject, spec, {})

        assert caught.value.kind == WorkflowRefusal.BAD_SPEC
        assert "nope" in str(caught.value)

    async def test_chat_only_tool_is_refused(
        self, service: WorkflowService, context: CallContext
    ) -> None:
        spec = "name: x\ntasks:\n  t: {tool: canvas_open, args: {path: p}}\n"
        with pytest.raises(WorkflowError) as caught:
            await service.save(context.subject, spec, {})

        assert "canvas_open" in str(caught.value)

    async def test_denied_tool_is_refused(
        self, store: WorkflowStore, probe: Probe, context: CallContext
    ) -> None:
        async def registry() -> ToolRegistry:
            return probe.registry(["echo"])

        limited = WorkflowService(
            store,
            registry,
            "test:0",
            MemoryMessageBus("test:0"),
            RunLocking(locks=MemoryLiveLocks("test:0", 20), heartbeat_sec=1.0),
        )
        with pytest.raises(WorkflowError) as caught:
            await limited.save(context.subject, PARALLEL, {})

        assert "slow" in str(caught.value)

    async def test_saved_and_listed(
        self, service: WorkflowService, context: CallContext
    ) -> None:
        stored = await service.save(context.subject, VALUES, {"x": 1})

        assert stored.name == "values"
        assert stored.tools == ("echo",)
        listed = await service.list_workflows(context.subject)
        assert [item.id for item in listed] == [stored.id]
        assert (await service.get_by_name(context.subject, "values")).id == stored.id

        stranger = context.subject.model_copy(update={"user_id": STRANGER})
        with pytest.raises(WorkflowError) as caught:
            await service.get_by_name(stranger, "values")

        assert caught.value.kind == WorkflowRefusal.NOT_FOUND


class TestRun:
    async def test_independent_tasks_run_together(
        self, service: WorkflowService, probe: Probe, context: CallContext
    ) -> None:
        stored = await service.save(context.subject, PARALLEL, {})

        outcome = await service.run(context, stored, service.new_run_id())

        assert outcome.state.status is RunStatus.DONE
        a, b, c = (outcome.state.tasks[name] for name in ("a", "b", "c"))
        assert a.result is not None
        assert a.result.kind == "text"
        assert a.started_at is not None
        assert b.started_at is not None
        assert abs((a.started_at - b.started_at).total_seconds()) < 0.2
        assert c.started_at is not None
        assert a.finished_at is not None
        assert b.finished_at is not None
        assert c.started_at >= max(a.finished_at, b.finished_at)
        assert outcome.results["c"].llm_text() == "done c"

        # задачи шли под областью запуска; инициатор — вызов модели в треде
        assert len(probe.contexts) == 3
        for seen in probe.contexts:
            assert seen.scope.kind is ScopeKind.WORKFLOW
            assert seen.scope.id == str(outcome.run.id)
            assert isinstance(seen.initiator, LlmInitiator)
            assert seen.initiator.thread_id == THREAD

        run = await service.get_run(context.subject, outcome.run.id)
        assert run.status is RunStatus.DONE
        assert run.state.tasks["c"].status is TaskStatus.DONE
        assert run.instance == "test:0"
        assert run.initiator["kind"] == "chat"

    async def test_value_edges_substitute_results(
        self, service: WorkflowService, context: CallContext
    ) -> None:
        stored = await service.save(context.subject, VALUES, {})

        outcome = await service.run(context, stored, service.new_run_id())

        assert outcome.state.status is RunStatus.DONE
        assert outcome.results["second"].llm_text() == "hello world"
        assert outcome.results["third"].llm_text() == "hello world"

    async def test_failure_skips_dependants(
        self, service: WorkflowService, context: CallContext
    ) -> None:
        stored = await service.save(context.subject, FAILING, {})

        outcome = await service.run(context, stored, service.new_run_id())

        assert outcome.state.status is RunStatus.FAILED
        assert outcome.state.tasks["boom"].status is TaskStatus.FAILED
        assert "broken" in outcome.state.tasks["boom"].error
        assert outcome.state.tasks["after"].status is TaskStatus.SKIPPED
        assert outcome.state.tasks["aside"].status is TaskStatus.DONE
        assert "after" not in outcome.results
        assert isinstance(outcome.results["boom"], ErrorResult)

    async def test_rerun_checks_grants_again(
        self, store: WorkflowStore, probe: Probe, context: CallContext
    ) -> None:
        granted = ["*"]

        async def registry() -> ToolRegistry:
            return probe.registry(granted)

        service = WorkflowService(
            store,
            registry,
            "test:0",
            MemoryMessageBus("test:0"),
            RunLocking(locks=MemoryLiveLocks("test:0", 20), heartbeat_sec=1.0),
        )
        stored = await service.save(context.subject, VALUES, {})
        granted[:] = ["slow"]

        with pytest.raises(WorkflowError):
            await service.run(context, stored, service.new_run_id())


def holder_locks(service: WorkflowService) -> MemoryLiveLocks:
    """Общие блокировки двух сервисов одного теста: чужой видит держателя."""
    locks = service.locks
    if not isinstance(locks, MemoryLiveLocks):
        raise TypeError("test services use MemoryLiveLocks")

    return locks


class TestStop:
    async def _running(self, service: WorkflowService, subject: Subject):
        for _ in range(100):
            runs = await service.list_runs(subject, 10)
            live = [run for run in runs if run.status is RunStatus.RUNNING]
            if live:
                return live[0]

            await asyncio.sleep(0.02)

        raise AssertionError("the run never showed up as running")

    async def test_stop_by_owner(
        self, service: WorkflowService, context: CallContext
    ) -> None:
        stored = await service.save(context.subject, LONG, {})
        running = asyncio.create_task(
            service.run(context, stored, service.new_run_id())
        )
        live = await self._running(service, context.subject)

        stranger = context.subject.model_copy(update={"user_id": STRANGER})
        assert await service.stop(stranger, live.id) is StopOutcome.FINISHED
        assert await service.stop(context.subject, live.id) is StopOutcome.STOPPED

        outcome = await asyncio.wait_for(running, 5)
        assert outcome.state.status is RunStatus.STOPPED
        assert outcome.state.tasks["wait"].status is TaskStatus.STOPPED
        assert outcome.state.tasks["then"].status is TaskStatus.STOPPED
        assert await service.stop(context.subject, live.id) is StopOutcome.FINISHED

        run = await service.get_run(context.subject, live.id)
        assert run.status is RunStatus.STOPPED
        assert run.finished_at is not None

    async def test_stop_from_another_instance_goes_through_the_bus(
        self, store: WorkflowStore, probe: Probe, context: CallContext
    ) -> None:
        """Чужой живой запуск: stop даёт ACCEPTED, команда доходит до держателя."""

        async def registry() -> ToolRegistry:
            return probe.registry(["*"])

        bus = MemoryMessageBus("shared")
        holder = WorkflowService(
            store,
            registry,
            "node1-studio",
            bus,
            RunLocking(locks=MemoryLiveLocks("node1-studio", 20), heartbeat_sec=1.0),
        )
        other = WorkflowService(
            store,
            registry,
            "node2-studio",
            bus,
            RunLocking(locks=holder_locks(holder), heartbeat_sec=1.0),
        )
        runner = CommandRunner(bus, "node1-studio")
        runner.start()
        try:
            stored = await holder.save(context.subject, LONG, {})
            running = asyncio.create_task(
                holder.run(context, stored, holder.new_run_id())
            )
            live = await self._running(holder, context.subject)

            assert await other.stop(context.subject, live.id) is StopOutcome.ACCEPTED

            outcome = await asyncio.wait_for(running, 5)
            assert outcome.state.status is RunStatus.STOPPED
        finally:
            runner.stop()

    async def test_stop_of_the_turn_stops_the_run(
        self, service: WorkflowService, context: CallContext
    ) -> None:
        stored = await service.save(context.subject, LONG, {})
        running = asyncio.create_task(
            service.run(context, stored, service.new_run_id())
        )
        await self._running(service, context.subject)

        context.cancellation.cancel(StopReason.USER_STOP)

        outcome = await asyncio.wait_for(running, 5)
        assert outcome.state.status is RunStatus.STOPPED


class TestTools:
    async def test_save_run_list_from_chat(
        self, service: WorkflowService, context: CallContext
    ) -> None:
        async def source() -> WorkflowService:
            return service

        by_name = {
            t.name: t for t in build_workflow_tools(WorkflowToolConfig(), source)
        }
        view = ToolCallViews.of("workflow_save")
        assert isinstance(view, ScriptCall)
        assert view.lang == "yaml"

        saved = await by_name["workflow_save"].ainvoke({"spec": VALUES})
        assert "saved" in saved

        listed = await by_name["workflow_list"].ainvoke({})
        assert "values" in listed

        message = await by_name["workflow_run"].ainvoke(
            {
                "name": "workflow_run",
                "args": {"name": "values"},
                "id": "c1",
                "type": "tool_call",
            }
        )
        report = message.artifact
        assert isinstance(report, MultiResult)
        assert report.ok
        assert report.metadata[ReportKey.STATUS] == "done"
        assert "third=done" in report.metadata[ReportKey.TASKS]
        summary, *rest = report.items
        assert "third: done" in summary.llm_text()
        assert [item.metadata[ReportKey.TASK] for item in rest] == [
            "first",
            "second",
            "third",
        ]

        missing = await by_name["workflow_run"].ainvoke(
            {
                "name": "workflow_run",
                "args": {"name": "nope"},
                "id": "c2",
                "type": "tool_call",
            }
        )
        assert isinstance(missing.artifact, ErrorResult)
        assert missing.artifact.error_kind == WorkflowRefusal.NOT_FOUND


async def test_draft_changes_reach_the_user_scope_and_save_drops_the_draft(
    service: WorkflowService, context: CallContext
) -> None:
    """Правка черновика уходит вкладкам пользователя с revision и sid автора; Save
    делает сохранённое истиной и снимает черновик.
    """
    seen: list[Envelope] = []

    async def collect(envelope: Envelope) -> None:
        seen.append(envelope)

    leave = service.bus.subscribe(Scope.user(context.subject.user_id), collect)
    try:
        stored = await service.save(context.subject, PARALLEL, {})
        draft = await service.put_draft(
            context.subject, stored.id, "name: broken\n", {"positions": {}}, "sid-1"
        )
        assert draft.draft_revision == 1
        assert draft.draft_spec == "name: broken\n"

        fetched = await service.get(context.subject, stored.id)
        assert fetched.draft_spec == "name: broken\n"

        cleared = await service.clear_draft(context.subject, stored.id, "sid-1")
        assert cleared.draft_spec is None
        assert cleared.draft_revision == 2

        await service.put_draft(context.subject, stored.id, "name: x\n", {}, "sid-2")
        saved = await service.save(context.subject, PARALLEL, {})
        assert saved.draft_spec is None
        assert saved.draft_revision == 4
    finally:
        leave()

    drafts: list[tuple[str, int, str, str]] = []
    for envelope in seen:
        message = envelope.message
        if not isinstance(message, WorkflowDraftChanged):
            continue

        drafts.append(
            (
                str(message.workflow_id),
                message.revision,
                message.by_sid,
                message.action.value,
            )
        )

    workflow_id = str(saved.id)
    assert drafts == [
        (workflow_id, 0, "", "deleted"),
        (workflow_id, 1, "sid-1", "updated"),
        (workflow_id, 2, "sid-1", "deleted"),
        (workflow_id, 3, "sid-2", "updated"),
        (workflow_id, 4, "", "deleted"),
    ]
