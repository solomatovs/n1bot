"""Связь независимой таблицы задач с историей и SQL-сборкой запроса."""

import asyncio
from collections.abc import Sequence
from contextlib import suppress
from dataclasses import dataclass
from functools import partial
from uuid import uuid4

import pytest
from pydantic import JsonValue, TypeAdapter

from boba.agent import models as m
from boba.agent.history import HistoryStore
from boba.agent.openai_validator import OpenAiRequestDraft
from boba.agent.records import RecordFactory
from boba.agent.tasks import TaskCoordinationError, TaskCoordinator
from boba.db.postgres import AsyncPostgresPool
from boba.tasks.models import (
    TaskCode,
    TaskCreate,
    TaskError,
    TaskOutcome,
    TaskServiceError,
    TaskStatus,
    WorkerIdentity,
)
from boba.tasks.service import TaskService
from boba.tasks.worker import TaskNotifications

pytestmark = [pytest.mark.anyio, pytest.mark.integration]


@dataclass(frozen=True)
class Dispatch:
    records: tuple[m.HistoryRecord, ...]
    commands: tuple[TaskCreate, ...]


class Stand:
    def __init__(self, history: HistoryStore, notifications: TaskNotifications) -> None:
        self.history = history
        self.session_id = "coordinator-" + uuid4().hex
        self.owner_id = "coordinator-owner-" + uuid4().hex
        self.factory = RecordFactory("test")
        self.input_operation_id = uuid4().hex
        self.message_adapter = TypeAdapter(tuple[dict[str, JsonValue], ...])
        self.tasks = TaskService(history.pool)
        self.notifications = notifications
        self.published: list[tuple[m.HistoryRecord, ...]] = []
        self.resumed = asyncio.Event()
        self.resumes = 0
        self.coordinator = self.new_coordinator()
        self.worker = WorkerIdentity(
            uuid4().hex, frozenset({"echo"}), owners=frozenset({self.owner_id})
        )

    def messages(self, draft: OpenAiRequestDraft) -> tuple[dict[str, JsonValue], ...]:
        return self.message_adapter.validate_python(draft.request["messages"])

    def new_coordinator(self) -> TaskCoordinator:
        return TaskCoordinator(
            self.session_id,
            self.owner_id,
            self.history,
            self.tasks,
            version="test",
            notifications=self.notifications,
            lock=asyncio.Lock(),
            poll_sec=0.05,
            publisher=self.publish,
            resume=self.resume,
        )

    async def publish(self, records: Sequence[m.HistoryRecord]) -> None:
        self.published.append(tuple(records))

    def resume(self) -> None:
        self.resumes += 1
        self.resumed.set()

    def batch(self, count: int = 1, *, name: str = "echo") -> Dispatch:
        assistant_id = uuid4().hex
        calls = []
        pending = []
        references = []
        commands = []
        for _ in range(count):
            call_id = uuid4().hex
            task_id = uuid4().hex
            references.append(m.ToolCallReferencePart(call_id=call_id))
            call = self.factory.make(
                m.ToolCallRecord,
                key=call_id,
                payload={
                    "assistant_record_id": assistant_id,
                    "name": name,
                    "arguments": {},
                    "task_id": task_id,
                },
            )
            calls.append(call)
            pending.append(
                self.factory.make(
                    m.ToolResultRecord,
                    key=call_id,
                    payload={"call_record_id": call.record_id, "status": "pending"},
                )
            )
            commands.append(
                TaskCreate(
                    task_id=task_id,
                    kind="echo",
                    payload={"text": "hello"},
                    run_timeout_sec=10,
                )
            )

        message = self.factory.make(
            m.AssistantMessageRecord,
            record_id=assistant_id,
            parent_operation_id=self.input_operation_id,
            payload={"content": references, "completion": "complete"},
        )
        records = self.factory.group(
            [message, *calls, *pending], uuid4().hex, message.parent_operation_id
        )
        return Dispatch(records, tuple(commands))

    async def poll(self, task_id: str) -> m.ToolCallRecord:
        batch = self.batch(name="task_status")
        records = []
        for record in batch.records:
            if isinstance(record, m.ToolCallRecord):
                data = record.model_dump()
                data["payload"]["task_id"] = None
                data["payload"]["arguments"] = {"task_id": task_id}
                record = m.ToolCallRecord.model_validate(data)

            records.append(record)

        saved = await self.history.append(self.session_id, records)
        for record in saved:
            if isinstance(record, m.ToolCallRecord):
                return record

        raise AssertionError("Polling batch has no call")


@pytest.fixture
async def stand(history, postgres):
    notifications = TaskNotifications(
        partial(AsyncPostgresPool.dedicated, postgres), retry_sec=0.02
    )
    await notifications.start()
    value = Stand(history, notifications)
    await history.create_session(
        value.session_id,
        value.owner_id,
        "/workspace",
        [
            value.factory.make(
                m.ModelRecord, payload={"provider": "test", "value": {"model": "test"}}
            ),
            value.factory.make(
                m.UserMessageRecord,
                operation_id=value.input_operation_id,
                payload={"content": [m.TextPart(text="Run tools")]},
            ),
        ],
    )
    try:
        yield value

    finally:
        await notifications.close()
        async with history.pool.connection() as connection:
            await connection.execute(
                "delete from boba.tasks where owner_id = %s", (value.owner_id,)
            )
            await connection.execute(
                "delete from boba.records where session_id = %s", (value.session_id,)
            )
            await connection.execute(
                "delete from boba.sessions where session_id = %s", (value.session_id,)
            )


class PausedTasks(TaskService):
    def __init__(self, pool) -> None:
        super().__init__(pool)
        self.inserted = asyncio.Event()
        self.release = asyncio.Event()

    async def add(self, owner_id, command, *, connection=None):
        state = await super().add(owner_id, command, connection=connection)
        self.inserted.set()
        await self.release.wait()
        return state


async def test_dispatch_commits_entire_tool_group_before_worker_can_claim(
    stand: Stand, agent_timeout
):
    batch = stand.batch(3)
    paused = PausedTasks(stand.history.pool)
    stand.coordinator.tasks = paused
    dispatching = asyncio.create_task(
        stand.coordinator.dispatch(batch.records, batch.commands)
    )
    try:
        async with asyncio.timeout(agent_timeout):
            await paused.inserted.wait()

        assert await stand.tasks.claim(stand.worker, uuid4(), 5) is None
        assert await stand.history.head(stand.session_id) == 2
        assert not stand.published
        paused.release.set()
        async with asyncio.timeout(agent_timeout):
            saved = await dispatching

        assert len(saved) == 7
        assert len(stand.published) == 1
        for record in saved:
            if isinstance(record, m.ToolResultRecord):
                assert record.payload.state is not None
                assert record.payload.state.status == "pending"
                assert record.payload.state.revision == 1

        draft = await stand.history.openai(stand.session_id)
        assert [item["role"] for item in stand.messages(draft)] == [
            "user",
            "assistant",
            "tool",
            "tool",
            "tool",
        ]
        assert await stand.tasks.claim(stand.worker, uuid4(), 5) is not None

    finally:
        paused.release.set()
        await dispatching


async def test_history_failure_rolls_back_every_task(stand: Stand):
    batch = stand.batch(2)
    broken = list(batch.records)
    call = m.ToolCallRecord.model_validate(broken[1])
    data = call.model_dump()
    data["payload"]["assistant_record_id"] = "missing-assistant"
    broken[1] = m.ToolCallRecord.model_validate(data)
    with pytest.raises(TaskCoordinationError):
        await stand.coordinator.dispatch(broken, batch.commands)

    assert await stand.history.head(stand.session_id) == 2
    assert not stand.published
    for command in batch.commands:
        with pytest.raises(TaskServiceError) as error:
            await stand.tasks.status(stand.owner_id, command.task_id)

        assert error.value.code is TaskCode.NOT_FOUND


async def test_conflicting_task_rolls_back_earlier_insert_and_history(stand: Stand):
    batch = stand.batch(2)
    conflict = batch.commands[1].model_copy(
        update={"payload": {"text": "another action"}}
    )
    await stand.tasks.add(stand.owner_id, conflict)
    with pytest.raises(TaskCoordinationError):
        await stand.coordinator.dispatch(batch.records, batch.commands)

    with pytest.raises(TaskServiceError):
        await stand.tasks.status(stand.owner_id, batch.commands[0].task_id)

    assert await stand.history.head(stand.session_id) == 2
    assert not stand.published


async def test_observations_and_retry_do_not_change_historical_request(stand: Stand):
    batch = stand.batch()
    initial = await stand.coordinator.dispatch(batch.records, batch.commands)
    task_id = batch.commands[0].task_id
    first = await stand.coordinator.observe(task_id)
    second = await stand.coordinator.observe(task_id)
    assert first.record_id != second.record_id
    assert first.payload == second.payload
    assert first.seq is not None
    before = await stand.history.openai(stand.session_id, through_seq=first.seq)
    lease = await stand.tasks.claim(stand.worker, uuid4(), 5)
    assert lease is not None
    await stand.tasks.finish(
        lease, TaskOutcome(status=TaskStatus.COMPLETED, result={"answer": 42})
    )
    observed = await stand.coordinator.reconcile()
    assert len(observed) == 1
    assert observed[0].payload.status == "completed"
    assert stand.resumes == 1
    assert await stand.coordinator.reconcile() == ()
    after = await stand.history.openai(stand.session_id, through_seq=first.seq)
    assert after.request == before.request
    head = await stand.history.head(stand.session_id)
    assert await stand.coordinator.dispatch(batch.records, batch.commands) == initial
    assert await stand.history.head(stand.session_id) == head
    assert (
        await stand.tasks.status(stand.owner_id, task_id)
    ).status is TaskStatus.COMPLETED


async def test_poll_pins_snapshot_and_always_writes_an_observation(stand: Stand):
    batch = stand.batch()
    await stand.coordinator.dispatch(batch.records, batch.commands)
    task_id = batch.commands[0].task_id
    lease = await stand.tasks.claim(stand.worker, uuid4(), 5)
    assert lease is not None
    call1 = await stand.poll(task_id)
    answer1 = await stand.coordinator.answer_status(task_id, call1)
    call2 = await stand.poll(task_id)
    answer2 = await stand.coordinator.answer_status(task_id, call2)
    assert answer1.payload.status == "completed"
    assert answer2.payload.status == "completed"
    assert answer1.payload.source_record_id != answer2.payload.source_record_id
    assert answer1.payload.source_record_id is not None
    first_draft = await stand.history.openai(stand.session_id)
    await stand.tasks.finish(
        lease,
        TaskOutcome(
            status=TaskStatus.FAILED,
            error=TaskError(code="echo_failed", message="Execution refused"),
        ),
    )
    await stand.coordinator.reconcile()
    draft = await stand.history.openai(stand.session_id)
    polls_before = stand.messages(first_draft)[-1]
    polls_after = stand.messages(draft)[-1]
    assert polls_after == polls_before
    call3 = await stand.poll(task_id)
    answer3 = await stand.coordinator.answer_status(task_id, call3)
    assert answer3.payload.status == "completed"
    rows = (await stand.history.read(stand.session_id)).rows
    snapshots = []
    for row in rows:
        if not isinstance(row, m.ToolResultRecord):
            continue

        if row.payload.state is not None:
            snapshots.append(row.payload.state)

    assert [snapshot.status for snapshot in snapshots] == [
        "pending",
        "running",
        "running",
        "failed",
        "failed",
    ]


async def test_poll_external_task_and_deny_another_owner(stand: Stand):
    command = TaskCreate(
        task_id=uuid4().hex, kind="echo", payload={}, run_timeout_sec=10
    )
    await stand.tasks.add(stand.owner_id, command)
    poll = await stand.poll(command.task_id)
    answer = await stand.coordinator.answer_status(command.task_id, poll)
    assert answer.payload.source_record_id is None
    assert answer.payload.status == "completed"
    assert answer.payload.state is not None
    assert answer.payload.state.status == "pending"
    assert (await stand.tasks.status(stand.owner_id, command.task_id)).revision == 1
    outsider = stand.new_coordinator()
    outsider.owner_id = "other-user"
    with pytest.raises(TaskCoordinationError):
        await outsider.observe(command.task_id)

    missing = await stand.poll("unavailable-task")
    failure = await stand.coordinator.answer_status("unavailable-task", missing)
    assert failure.payload.status == "failed"
    assert failure.payload.state is None


async def test_reopened_coordinator_observes_completion_without_notification(
    stand: Stand, agent_timeout
):
    batch = stand.batch()
    await stand.coordinator.dispatch(batch.records, batch.commands)
    lease = await stand.tasks.claim(stand.worker, uuid4(), 5)
    assert lease is not None
    await stand.tasks.finish(
        lease, TaskOutcome(status=TaskStatus.COMPLETED, result="done")
    )
    reopened = stand.new_coordinator()
    observer = asyncio.create_task(reopened.run())
    try:
        async with asyncio.timeout(agent_timeout):
            await reopened.ready.wait()
            await stand.resumed.wait()

        assert stand.resumes == 1
        draft = await stand.history.openai(stand.session_id)
        assert stand.messages(draft)[-1]["role"] == "tool"

    finally:
        observer.cancel()
        with suppress(asyncio.CancelledError):
            await observer


@pytest.mark.parametrize("delivery", ["notify", "poll"])
async def test_live_notification_and_periodic_reconciliation_do_not_wake_for_running(
    stand: Stand, agent_timeout, delivery
):
    if delivery == "notify":
        stand.coordinator.poll_sec = agent_timeout * 2

    observer = asyncio.create_task(stand.coordinator.run())
    try:
        async with asyncio.timeout(agent_timeout):
            await stand.coordinator.ready.wait()

        batch = stand.batch()
        await stand.coordinator.dispatch(batch.records, batch.commands)
        lease = await stand.tasks.claim(stand.worker, uuid4(), 5)
        assert lease is not None
        await stand.coordinator.reconcile()
        assert stand.resumes == 0
        if delivery == "poll":
            await stand.notifications.close()

        await stand.tasks.finish(
            lease, TaskOutcome(status=TaskStatus.COMPLETED, result="done")
        )
        async with asyncio.timeout(agent_timeout):
            await stand.resumed.wait()

        assert stand.resumes == 1
        assert await stand.coordinator.reconcile() == ()

    finally:
        observer.cancel()
        with suppress(asyncio.CancelledError):
            await observer


async def test_observation_targets_copied_call_after_compaction(stand: Stand):
    batch = stand.batch()
    original = await stand.coordinator.dispatch(batch.records, batch.commands)
    boundary = stand.factory.make(
        m.CompactMessageRecord,
        payload={"reason": "manual", "content": [m.TextPart(text="Summary")]},
    )
    model = (await stand.history.latest(stand.session_id, kind="model"))[0]
    copies = stand.factory.copy([model, *original])
    saved = await stand.history.append(stand.session_id, [boundary, *copies])
    copied_call = m.ToolCallRecord.model_validate(saved[3])
    assert copied_call.copied_from_id == original[1].record_id
    task_id = batch.commands[0].task_id
    observed = await stand.coordinator.observe(task_id)
    assert observed.payload.call_record_id == copied_call.record_id
    assert observed.payload.state is not None
    assert observed.payload.state.task_id == task_id
    draft = await stand.history.openai(stand.session_id)
    assert len(stand.messages(draft)) == 3
