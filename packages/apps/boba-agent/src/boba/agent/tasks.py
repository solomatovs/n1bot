"""Постановка задач вместе с историей и сохранение прочитанных состояний.

Ошибки:
TaskCoordinationError — постановка, чтение или сохранение наблюдения не завершились;
    исходная ошибка доступна через __cause__.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncGenerator, Awaitable, Callable, Iterator, Sequence
from contextlib import asynccontextmanager
from math import isfinite
from uuid import uuid4

from boba.agent import models as m
from boba.agent.history import HistoryStore, HistoryTransaction
from boba.agent.queries import Query
from boba.agent.records import RecordFactory
from boba.tasks.models import (
    TaskChanged,
    TaskCode,
    TaskCreate,
    TaskServiceError,
    TaskState,
    TaskStatus,
)
from boba.tasks.service import TaskService
from boba.tasks.worker import TaskNotifications

logger = logging.getLogger(__name__)


class TaskCoordinationError(RuntimeError):
    """Ошибка адаптера задач к истории; worker не получает эту ошибку."""


class ToolTaskBinding(m.DataModel):
    """Результат SQL: действующий вызов и последнее сохранённое наблюдение."""

    call: m.ToolCallRecord
    result: m.ToolResultRecord

    @property
    def task_id(self) -> str:
        task_id = self.call.payload.task_id
        if task_id is None:
            raise TaskCoordinationError(
                f"Tool call {self.call.record_id}: expected a task_id"
            )

        return task_id

    def changed(self, state: TaskState) -> bool:
        previous = self.result.payload.state
        if previous is None:
            return True

        if previous.revision > state.revision:
            raise TaskCoordinationError(
                f"Task {state.task_id}: table revision {state.revision} precedes "
                f"history revision {previous.revision}"
            )

        return previous.revision != state.revision

    @property
    def unfinished(self) -> bool:
        return self.result.payload.status in (TaskStatus.PENDING, TaskStatus.RUNNING)


class TaskCoordinator:
    """Адаптер одной сессии к TaskService: добавляет задания и пишет снимки в records.

    Владелец вызывает run для подписки и сверки, dispatch для атомарной постановки;
    publisher получает сохранённые строки, resume — сигнал о новом завершении.
    Координатор не захватывает и не исполняет задачи.
    """

    def __init__(
        self,
        session_id: str,
        owner_id: str,
        store: HistoryStore,
        tasks: TaskService,
        *,
        version: str,
        notifications: TaskNotifications,
        lock: asyncio.Lock,
        poll_sec: float = 5,
        publisher: Callable[[Sequence[m.HistoryRecord]], Awaitable[None]] | None = None,
        resume: Callable[[], None] | None = None,
    ) -> None:
        if not session_id:
            raise TaskCoordinationError(
                "Creating task coordinator: session_id is empty"
            )

        if not owner_id:
            raise TaskCoordinationError("Creating task coordinator: owner_id is empty")

        valid_interval = isfinite(poll_sec) and poll_sec > 0
        if not valid_interval:
            raise TaskCoordinationError(
                "Creating task coordinator: poll_sec must be finite and positive"
            )

        self.session_id = session_id
        self.owner_id = owner_id
        self.store = store
        self.tasks = tasks
        self.records = RecordFactory(version)
        self.notifications = notifications
        self.lock = lock
        self.poll_sec = poll_sec
        self.publisher = publisher
        self.resume = resume
        self.ready = asyncio.Event()
        self._changed = asyncio.Event()
        self._subscriptions: dict[str, Callable[[], None]] = {}
        self._running = False

    @asynccontextmanager
    async def _operation(self, action: str) -> AsyncGenerator[None, None]:
        try:
            yield

        except TaskCoordinationError:
            raise

        except Exception as exc:
            raise TaskCoordinationError(
                f"Session {self.session_id}: {action} failed: {exc}"
            ) from exc

    def _dispatch_calls(
        self,
        records: Sequence[m.HistoryRecord],
        commands: Sequence[TaskCreate],
    ) -> dict[str, m.ToolCallRecord]:
        definitions: dict[str, TaskCreate] = {}
        for command in commands:
            definitions[command.task_id] = command

        if len(definitions) != len(commands):
            raise TaskCoordinationError("Dispatch requires distinct task IDs")

        calls: dict[str, m.ToolCallRecord] = {}
        task_ids: set[str] = set()
        for record in records:
            if not isinstance(record, m.ToolCallRecord):
                continue

            task_id = record.payload.task_id
            if task_id is None:
                raise TaskCoordinationError(
                    f"Dispatch call {record.record_id} has no task_id"
                )

            if task_id not in definitions:
                raise TaskCoordinationError(
                    f"Dispatch call {record.record_id}: task {task_id} is missing"
                )

            if task_id in task_ids:
                raise TaskCoordinationError(
                    f"Dispatch task {task_id} belongs to multiple calls"
                )

            calls[record.record_id] = record
            task_ids.add(task_id)

        if task_ids != definitions.keys():
            raise TaskCoordinationError("Dispatch contains tasks without tool calls")

        return calls

    def _initial_records(
        self, records: Sequence[m.HistoryRecord], commands: Sequence[TaskCreate]
    ) -> Iterator[m.HistoryRecord]:
        calls = self._dispatch_calls(records, commands)
        results: set[str] = set()
        for record in records:
            if not isinstance(record, m.ToolResultRecord):
                yield record
                continue

            call = calls.get(record.payload.call_record_id)
            if call is None:
                raise TaskCoordinationError(
                    f"Initial result {record.record_id}: call is missing"
                )

            if record.payload.status != TaskStatus.PENDING:
                raise TaskCoordinationError(
                    f"Initial result {record.record_id}: expected pending"
                )

            if record.payload.source_record_id is not None:
                raise TaskCoordinationError(
                    f"Initial result {record.record_id}: expected own snapshot"
                )

            if call.record_id in results:
                raise TaskCoordinationError(
                    f"Dispatch call {call.record_id} has repeated initial results"
                )

            results.add(call.record_id)
            snapshot = m.TaskSnapshot.model_validate(
                {
                    "task_id": call.payload.task_id,
                    "revision": 1,
                    "description": call.payload.name,
                    "status": TaskStatus.PENDING,
                }
            )
            data = record.model_dump()
            data["payload"] = m.ToolResultRecord.Payload(
                call_record_id=call.record_id,
                status="pending",
                state=snapshot,
                content=self._content(snapshot),
            )
            yield m.ToolResultRecord.model_validate(data)

        if results != calls.keys():
            raise TaskCoordinationError(
                "Every dispatched call requires an initial pending result"
            )

    async def dispatch(
        self, records: Sequence[m.HistoryRecord], commands: Sequence[TaskCreate]
    ) -> tuple[m.HistoryRecord, ...]:
        """Сохранить пачку ответа и задания одной транзакцией, без запуска worker."""
        async with self._operation("dispatching tasks"):
            prepared = tuple(self._initial_records(records, commands))
            async with self.lock:
                async with self.store.transaction(self.session_id) as transaction:
                    await self._require_owner(transaction)
                    for command in commands:
                        await self.tasks.add(
                            self.owner_id, command, connection=transaction.connection
                        )

                    saved = await transaction.append(prepared)

                self._changed.set()
                await self._publish(saved)

            return saved

    async def _require_owner(self, transaction: HistoryTransaction) -> None:
        rows = await transaction.rows(Query.SESSION_GET, self.session_id, self.owner_id)
        if not rows:
            raise TaskCoordinationError(
                f"Session {self.session_id}: owner has no access"
            )

    async def _bindings(
        self, transaction: HistoryTransaction, task_id: str | None = None
    ) -> tuple[ToolTaskBinding, ...]:
        rows = await transaction.rows(Query.TOOL_TASKS, self.session_id, task_id)
        return tuple(map(ToolTaskBinding.model_validate, rows))

    def _binding(
        self, bindings: Sequence[ToolTaskBinding], task_id: str
    ) -> ToolTaskBinding | None:
        if not bindings:
            return None

        if len(bindings) != 1:
            raise TaskCoordinationError(
                f"Session {self.session_id}: task {task_id} has multiple calls"
            )

        return bindings[0]

    def _snapshot(self, state: TaskState, description: str) -> m.TaskSnapshot:
        error = None
        if state.error is not None:
            error = f"{state.error.code}: {state.error.message}"

        return m.TaskSnapshot.model_validate(
            {
                "task_id": state.task_id,
                "revision": state.revision,
                "description": description,
                "status": state.status,
                "result": state.result,
                "error": error,
            }
        )

    def _content(self, snapshot: m.TaskSnapshot) -> tuple[m.TextPart, ...]:
        # Текст наблюдения сохраняется один раз; SQL забирает его без рендеринга.
        return (m.TextPart(text=snapshot.model_dump_json()),)

    def _observation(
        self, call: m.ToolCallRecord, state: TaskState
    ) -> m.ToolResultRecord:
        snapshot = self._snapshot(state, call.payload.name)
        content: tuple[m.ToolResultPart, ...] = self._content(snapshot)
        if state.progress is not None:
            progress = json.dumps(state.progress, ensure_ascii=False)
            content += (m.TextPart(text=f"Progress: {progress}"),)

        return self.records.make(
            m.ToolResultRecord,
            key=call.key,
            parent_operation_id=call.operation_id,
            payload={
                "call_record_id": call.record_id,
                "status": state.status,
                "state": snapshot,
                "content": content,
            },
        )

    async def _publish(self, records: Sequence[m.HistoryRecord]) -> None:
        if self.publisher is not None:
            await self.publisher(records)

    def _resume(self) -> None:
        if self.resume is not None:
            self.resume()

    async def _observe(self, task_id: str, *, force: bool) -> m.ToolResultRecord | None:
        async with self.lock:
            async with self.store.transaction(self.session_id) as transaction:
                await self._require_owner(transaction)
                state = await self.tasks.status(
                    self.owner_id, task_id, connection=transaction.connection
                )
                bindings = await self._bindings(transaction, task_id)
                binding = self._binding(bindings, task_id)
                if binding is None:
                    raise TaskCoordinationError(
                        f"Session {self.session_id}: no call for task {state.task_id}"
                    )

                if not force:
                    if not binding.changed(state):
                        return None

                observation = self._observation(binding.call, state)
                saved = await transaction.append([observation])

            await self._publish(saved)
            if binding.unfinished and state.status.terminal:
                self._resume()

            return m.ToolResultRecord.model_validate(saved[0])

    async def observe(self, task_id: str) -> m.ToolResultRecord:
        """Каждый явный опрос сохраняет снимок, даже если revision не изменилась."""
        async with self._operation(f"observing task {task_id}"):
            result = await self._observe(task_id, force=True)
            if result is None:
                raise TaskCoordinationError(
                    f"Task {task_id}: explicit observation was not saved"
                )

            return result

    async def answer_status(
        self, task_id: str, call: m.ToolCallRecord
    ) -> m.ToolResultRecord:
        """Сохранить снимок и отдельный ответ вызову task_status в одной транзакции."""
        async with self._operation(f"answering task_status for {task_id}"):
            if call.payload.name != "task_status":
                raise TaskCoordinationError(
                    f"Call {call.record_id}: expected task_status"
                )

            async with self.lock:
                async with self.store.transaction(self.session_id) as transaction:
                    await self._require_owner(transaction)
                    state = None
                    failure = None
                    try:
                        state = await self.tasks.status(
                            self.owner_id, task_id, connection=transaction.connection
                        )

                    except TaskServiceError as exc:
                        if exc.code is not TaskCode.NOT_FOUND:
                            raise

                        failure = str(exc)

                    found = await transaction.rows(
                        Query.TOOL_CALLS,
                        self.session_id,
                        call.payload.assistant_record_id,
                    )
                    self._require_status_call(found, call)
                    bindings = await self._bindings(transaction, task_id)
                    binding = self._binding(bindings, task_id)
                    records = self._status_records(call, binding, state, failure)
                    grouped = self.records.group(
                        records, uuid4().hex, call.operation_id
                    )
                    saved = await transaction.append(grouped)

                await self._publish(saved)

            return m.ToolResultRecord.model_validate(saved[-1])

    def _require_status_call(
        self, rows: Sequence[dict], call: m.ToolCallRecord
    ) -> None:
        for row in rows:
            stored = m.ToolCallRecord.model_validate(row["record"])
            if stored == call:
                return

        raise TaskCoordinationError(
            f"Session {self.session_id}: polling call {call.record_id} is not stored"
        )

    def _status_records(
        self,
        call: m.ToolCallRecord,
        binding: ToolTaskBinding | None,
        state: TaskState | None,
        failure: str | None,
    ) -> tuple[m.ToolResultRecord, ...]:
        if state is None:
            return (
                self.records.make(
                    m.ToolResultRecord,
                    key=call.key,
                    payload={
                        "call_record_id": call.record_id,
                        "status": "failed",
                        "content": [
                            m.TextPart(text=f"Reading task status failed: {failure}")
                        ],
                    },
                ),
            )

        if binding is None:
            snapshot = self._snapshot(state, state.kind)
            return (
                self.records.make(
                    m.ToolResultRecord,
                    key=call.key,
                    payload={
                        "call_record_id": call.record_id,
                        "status": "completed",
                        "state": snapshot,
                        "content": self._content(snapshot),
                    },
                ),
            )

        if binding.call.record_id == call.record_id:
            raise TaskCoordinationError(
                f"Polling call {call.record_id} cannot observe itself"
            )

        observation = self._observation(binding.call, state)
        result = self.records.make(
            m.ToolResultRecord,
            key=call.key,
            payload={
                "call_record_id": call.record_id,
                "status": "completed",
                "source_record_id": observation.record_id,
            },
        )
        return observation, result

    def _notify(self, change: TaskChanged | None) -> None:
        self._changed.set()

    def _subscribe(self, bindings: Sequence[ToolTaskBinding]) -> None:
        active: set[str] = set()
        for binding in bindings:
            if not binding.unfinished:
                continue

            active.add(binding.task_id)
            if binding.task_id in self._subscriptions:
                continue

            self._subscriptions[binding.task_id] = self.notifications.subscribe(
                binding.task_id, self._notify
            )

        for task_id in tuple(self._subscriptions):
            if task_id in active:
                continue

            self._subscriptions.pop(task_id)()

    async def reconcile(self) -> tuple[m.ToolResultRecord, ...]:
        """Восстановить подписки и сверить незавершённые вызовы с таблицей задач."""
        async with self._operation("reconciling task observations"):
            async with self.store.transaction(self.session_id) as transaction:
                await self._require_owner(transaction)
                bindings = await self._bindings(transaction)

            self._subscribe(bindings)
            observations = []
            for binding in bindings:
                if not binding.unfinished:
                    continue

                observation = await self._observe(binding.task_id, force=False)
                if observation is not None:
                    observations.append(observation)

            return tuple(observations)

    def _retryable(self, cause: BaseException | None) -> bool:
        if isinstance(cause, TaskServiceError):
            return cause.code is TaskCode.STORAGE

        if isinstance(cause, m.HistoryWriteError):
            return cause.code in ("storage_unavailable", "commit_unknown")

        return False

    async def run(self) -> None:
        """Цикл наблюдения для TaskGroup владельца; отмена снимает только подписки."""
        async with self._operation("running task observation"):
            if self._running:
                raise TaskCoordinationError(
                    f"Session {self.session_id}: coordinator is already running"
                )

            self._running = True
            self.ready.clear()
            try:
                await self.notifications.wait_connected()
                while True:
                    self._changed.clear()
                    try:
                        await self.reconcile()
                        self.ready.set()

                    except TaskCoordinationError as exc:
                        cause = exc.__cause__
                        if not self._retryable(cause):
                            raise

                        logger.warning("Task observation will retry: %s", exc)

                    try:
                        async with asyncio.timeout(self.poll_sec):
                            await self._changed.wait()

                    except TimeoutError:
                        continue

            finally:
                for unsubscribe in self._subscriptions.values():
                    unsubscribe()

                self._subscriptions.clear()
                self._running = False
