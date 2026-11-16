"""Исполнение сессии: сохранение, принятие ввода, SQL-запрос, LLM и tools."""

from __future__ import annotations

import asyncio
import logging
import time
from abc import abstractmethod
from collections.abc import Awaitable, Callable, Sequence
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import partial
from typing import Any, Literal, Protocol, TypeVar
from uuid import uuid4

from boba.agent import models as m
from boba.agent.history import HistoryStore
from boba.agent.llm import Generation

logger = logging.getLogger(__name__)
Send = Callable[[str, dict[str, Any]], Awaitable[None]]
R = TypeVar("R", bound=m.RecordBase)


@dataclass(frozen=True)
class TextDelta:
    """Несохранённый текст ответа; протокольный адаптер выбирает формат чанка."""

    record_id: str
    position: int
    kind: Literal["text", "reasoning"]
    text: str


@dataclass(frozen=True)
class SavedBatch:
    session_id: str
    after_seq: int
    through_seq: int
    notices: tuple[str, ...]
    partial: tuple[TextDelta, ...]


@dataclass(frozen=True)
class SessionSnapshot:
    session_id: str
    through_seq: int
    after_seq: int
    replay_history: bool
    running: bool
    partial: tuple[TextDelta, ...]


class SessionProtocol(Protocol):
    """Проекция истории и текущего исполнения в уведомления одного протокола."""

    @abstractmethod
    async def initial(self, snapshot: SessionSnapshot) -> list[tuple[str, dict]]: ...

    @abstractmethod
    async def saved(self, batch: SavedBatch) -> list[tuple[str, dict]]: ...

    @abstractmethod
    async def options(self, session_id: str) -> list[tuple[str, dict]]: ...

    @abstractmethod
    def chunk(self, session_id: str, delta: TextDelta) -> tuple[str, dict]: ...

    @abstractmethod
    def state(self, session_id: str, running: bool) -> list[tuple[str, dict]]: ...

    @abstractmethod
    async def request_action(
        self, session_id: str, record: m.HistoryRecord
    ) -> dict: ...


@dataclass(frozen=True)
class AttemptContext:
    """Идентификаторы и время одной HTTP-попытки; ответ получает отдельную операцию."""

    number: int
    operation_id: str
    answer_operation_id: str
    output_id: str
    parent_operation_id: str | None
    service: bool
    started_at: datetime
    started: float


class RecordFactory:
    """Создаёт записи производителя; operation_id объединяет части, seq назначает хранилище."""

    def __init__(self, version: str) -> None:
        self.version = version

    def make(self, model: type[R], *, payload: Any = None, **fields: Any) -> R:
        values: dict[str, Any] = {
            "record_id": uuid4().hex,
            "operation_id": uuid4().hex,
            "operation_position": 0,
            "parent_operation_id": None,
            "created_at": datetime.now(UTC),
            "version": self.version,
            "payload": {},
        }
        for field in ("role", "key"):
            if model.model_fields[field].is_required():
                values[field] = None

        if payload is not None:
            values["payload"] = payload

        values.update(fields)
        return model.model_validate(values)

    def group(
        self,
        records: Sequence[m.HistoryRecord],
        operation_id: str,
        parent_operation_id: str | None = None,
    ) -> tuple[m.HistoryRecord, ...]:
        values = []
        for position, item in enumerate(records):
            data = item.model_dump()
            data.update(
                operation_id=operation_id,
                operation_position=position,
                parent_operation_id=parent_operation_id,
            )
            values.append(m.HISTORY_RECORD_ADAPTER.validate_python(data))

        return tuple(values)

    def attempt(
        self, context: AttemptContext, output: Generation, failure: BaseException | None
    ) -> m.LlmRequestAttemptRecord | None:
        if output.address is None:
            return None

        status = "completed"
        level = "info"
        error = None
        text = "Inference attempt completed."
        if failure is not None:
            status = "failed"
            level = "warning"
            error = f"{type(failure).__name__}: {failure}"
            text = f"Inference attempt failed: {error}"

        if isinstance(failure, asyncio.CancelledError):
            status = "cancelled"

        return self.make(
            m.LlmRequestAttemptRecord,
            operation_id=context.operation_id,
            parent_operation_id=context.parent_operation_id,
            payload={
                "attempt": context.number,
                "status": status,
                "started_at": context.started_at,
                "method": "POST",
                "url": output.address,
                "status_code": output.status,
                "duration_ms": int((time.monotonic() - context.started) * 1000),
                "error": error,
                "level": level,
                "content": [m.TextPart(text=text)],
            },
        )

    def response(
        self, context: AttemptContext, output: Generation, failure: BaseException | None
    ) -> tuple[m.HistoryRecord, ...]:
        completion = "complete"
        if output.finish_reason == "length":
            completion = "output_limit"
        if failure is not None:
            completion = "cancelled"

        content = output.content(partial=completion != "complete")
        answers: list[m.HistoryRecord] = []
        if content:
            model: type[m.AssistantMessageRecord] | type[m.RequestOutputRecord] = (
                m.AssistantMessageRecord
            )
            if context.service:
                model = m.RequestOutputRecord

            answers.append(
                self.make(
                    model,
                    record_id=context.output_id,
                    parent_operation_id=context.operation_id,
                    payload={"content": content, "completion": completion},
                )
            )
            if completion == "complete":
                answers.extend(self.invocations(context.output_id, output))

        if output.usage is not None:
            answers.append(
                self.make(
                    m.TokenUsageRecord,
                    parent_operation_id=context.operation_id,
                    payload={"usage": output.usage},
                )
            )

        return self.group(answers, context.answer_operation_id, context.operation_id)

    def invocations(
        self, output_id: str, output: Generation
    ) -> tuple[m.HistoryRecord, ...]:
        records: list[m.HistoryRecord] = []
        for call in output.calls():
            invocation = self.make(
                m.ToolCallRecord,
                key=call.id,
                payload={
                    "assistant_record_id": output_id,
                    "name": call.name,
                    "arguments": dict(call.arguments),
                },
            )
            records.append(invocation)
            records.append(
                self.make(
                    m.ToolResultRecord,
                    key=call.id,
                    payload={
                        "call_record_id": invocation.record_id,
                        "status": "pending",
                        "content": [
                            m.TextPart(text=f"Call {call.id}: pending execution.")
                        ],
                    },
                )
            )

        return tuple(records)

    def copy(self, records: Sequence[m.HistoryRecord]) -> tuple[m.HistoryRecord, ...]:
        identifiers = {}
        operations = {}
        positions: dict[str, int] = {}
        for item in records:
            identifiers[item.record_id] = uuid4().hex
            if item.operation_id not in operations:
                operations[item.operation_id] = uuid4().hex
                positions[item.operation_id] = 0

        copied = []
        for item in records:
            value = item.model_dump()
            value.update(
                session_id=None,
                seq=None,
                record_id=identifiers[item.record_id],
                copied_from_id=item.record_id,
                operation_id=operations[item.operation_id],
                operation_position=positions[item.operation_id],
                parent_operation_id=operations.get(
                    item.parent_operation_id, item.parent_operation_id
                ),
            )
            positions[item.operation_id] += 1
            payload = value["payload"]
            for field in (
                "assistant_record_id",
                "call_record_id",
                "source_record_id",
                "catalog_revision",
                "related_message_id",
            ):
                reference = payload.get(field)
                if reference in identifiers:
                    payload[field] = identifiers[reference]

            copied.append(m.HISTORY_RECORD_ADAPTER.validate_python(value))

        return tuple(copied)


@dataclass(frozen=True)
class PreparedRequest:
    operation_id: str | None
    through_seq: int
    input_seq: int = 0


class Subscription:
    """Ограничен только исходящий поток; входящие сообщения здесь не хранятся."""

    def __init__(
        self,
        send: Send,
        initial: list[tuple[str, dict]],
        capacity: int,
        protocol: SessionProtocol,
        disconnect=None,
    ) -> None:
        self.protocol = protocol
        self.pending: asyncio.Queue = asyncio.Queue(capacity)
        self.failed = False
        self.disconnect = disconnect
        self.disconnect_task = None
        self.ready: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self.task = asyncio.create_task(self._send(send, initial))

    async def _send(self, send: Send, initial: list[tuple[str, dict]]) -> None:
        try:
            for method, params in initial:
                await send(method, params)

            self.ready.set_result(None)

            while True:
                method, params = await self.pending.get()
                try:
                    await send(method, params)
                finally:
                    self.pending.task_done()
        except asyncio.CancelledError:
            self.ready.cancel()
            raise

        except Exception as exc:
            if not self.ready.done():
                self.ready.set_exception(exc)

            logger.exception("ACP subscriber disconnected")
        finally:
            self.failed = True

    def publish(self, method: str, params: dict) -> None:
        if self.task.done():
            return
        try:
            self.pending.put_nowait((method, params))
        except asyncio.QueueFull:
            self.failed = True
            self.task.cancel()
            if self.disconnect is not None:
                self.disconnect_task = asyncio.create_task(self.disconnect())

    async def close(self) -> None:
        self.task.cancel()
        with suppress(asyncio.CancelledError):
            await self.task
        if self.disconnect_task is not None:
            await self.disconnect_task

    async def flush(self) -> None:
        """Дождаться отправки уже опубликованных обновлений перед ответом на prompt v1."""
        drained = asyncio.create_task(self.pending.join())
        try:
            done, _ = await asyncio.wait(
                (drained, self.task), return_when=asyncio.FIRST_COMPLETED
            )
            if self.task in done:
                raise ConnectionError(
                    "ACP subscription closed before pending updates were sent"
                )

            await drained
        finally:
            drained.cancel()
            with suppress(asyncio.CancelledError):
                await drained


class Agent:
    """Один исполнитель сессии; все клиенты используют один экземпляр."""

    def __init__(
        self,
        session_id: str,
        store: HistoryStore,
        llm,
        tools,
        *,
        version: str,
        max_steps: int = 32,
        subscriber_buffer: int = 256,
    ) -> None:
        self.session_id, self.store, self.llm, self.tools = (
            session_id,
            store,
            llm,
            tools,
        )
        self.version, self.max_steps, self.subscriber_buffer = (
            version,
            max_steps,
            subscriber_buffer,
        )
        self.records = RecordFactory(version)
        self.changed = asyncio.Event()
        self.idle = asyncio.Event()
        self.idle.set()
        self.lock = asyncio.Lock()
        self.subscribers: set[Subscription] = set()
        self.partial: dict[int, TextDelta] = {}
        self.worker: asyncio.Task | None = None
        self.operation: asyncio.Task | None = None
        self.closed = False
        self.last_error: Exception | None = None
        self.background: set[asyncio.Task] = set()
        self.continue_requested = False

    def make(self, model: type[R], **fields) -> R:
        return self.records.make(model, **fields)

    async def append(self, records: Sequence[m.HistoryRecord], *, expected_seq=None):
        async with self.lock:
            saved = await self.store.append(
                self.session_id, records, expected_seq=expected_seq
            )
            await self._publish_saved(saved)
            return saved

    def _protocols(self) -> dict[type, SessionProtocol]:
        projections: dict[type, SessionProtocol] = {}
        for subscriber in self.subscribers:
            protocol = subscriber.protocol
            projections[type(protocol)] = protocol

        return projections

    async def _publish_saved(self, saved) -> None:
        notices = []
        for item in saved:
            if isinstance(item, (m.NoticeRecord, m.LlmRequestAttemptRecord)):
                notices.append(item.record_id)

        batch = SavedBatch(
            self.session_id,
            saved[0].seq - 1,
            saved[-1].seq,
            tuple(notices),
            tuple(self.partial.values()),
        )
        for protocol in self._protocols().values():
            frames = await protocol.saved(batch)
            self._publish(frames, protocol)

    async def options_changed(self) -> None:
        for protocol in self._protocols().values():
            updates = await protocol.options(self.session_id)
            self._publish(updates, protocol)

    def _publish(
        self, frames: Sequence[tuple[str, dict]], protocol: SessionProtocol
    ) -> None:
        for subscriber in tuple(self.subscribers):
            if type(subscriber.protocol) is not type(protocol):
                continue

            for method, params in frames:
                subscriber.publish(method, params)

            if subscriber.failed:
                self.subscribers.discard(subscriber)

    def _state(self, running: bool) -> None:
        for protocol in self._protocols().values():
            frames = protocol.state(self.session_id, running)
            self._publish(frames, protocol)

    async def subscribe(
        self,
        send: Send,
        protocol: SessionProtocol,
        *,
        replay_history: bool = True,
        after_seq: int = 0,
        disconnect=None,
    ) -> Subscription:
        async with self.lock:
            head = await self.store.head(self.session_id)
            snapshot = SessionSnapshot(
                self.session_id,
                head,
                after_seq,
                replay_history,
                not self.idle.is_set(),
                tuple(self.partial.values()),
            )
            initial = await protocol.initial(snapshot)
            subscriber = Subscription(
                send, initial, self.subscriber_buffer, protocol, disconnect
            )
            self.subscribers.add(subscriber)

        try:
            await asyncio.shield(subscriber.ready)

        except BaseException:
            self.subscribers.discard(subscriber)
            await subscriber.close()
            raise

        return subscriber

    async def submit(
        self,
        content: Sequence[m.ContentPart],
        *,
        record_id: str | None = None,
        settings: Sequence[m.ParameterRecord | m.ParameterRemovedRecord] = (),
    ) -> str:
        if self.closed:
            raise RuntimeError("Agent is closed")
        fields = {} if record_id is None else {"record_id": record_id}
        value = self.make(m.UserMessageRecord, payload={"content": content}, **fields)
        inputs: list[m.HistoryRecord] = [value]
        streaming = self.make(m.StreamingRecord, payload={"value": {"stream": True}})
        inputs.append(streaming)
        for setting in settings:
            if setting.scope != "request":
                raise ValueError("Input overrides require scope=request")

            inputs.append(m.HISTORY_RECORD_ADAPTER.validate_python(setting))

        records = self.records.group(inputs, value.operation_id)
        await self.append(records)
        self.wake()
        return value.record_id

    def wake(self, *, continue_tools: bool = False) -> None:
        if continue_tools:
            self.continue_requested = True

        self.idle.clear()
        self.changed.set()
        if self.worker is None:
            self.worker = asyncio.create_task(
                self._run(), name=f"agent:{self.session_id}"
            )

    async def _prepare(self, *, continue_tools: bool) -> PreparedRequest | None:
        async with self.lock, self.store.transaction(self.session_id) as transaction:
            pending = await transaction.pending_messages()
            if not pending:
                if not continue_tools and not self.continue_requested:
                    return None

            self.continue_requested = False

            if pending:
                latest = pending[-1]
                if latest.seq is None:
                    raise ValueError("Stored input is missing seq")

                return PreparedRequest(
                    latest.operation_id, await transaction.head(), latest.seq
                )

            streaming = self.make(
                m.StreamingRecord, payload={"value": {"stream": True}}
            )
            await transaction.append([streaming])
            return PreparedRequest(streaming.operation_id, await transaction.head())

    async def _run(self) -> None:
        while not self.closed:
            await self.changed.wait()
            self.changed.clear()
            self.last_error = None
            self._state(True)
            self.operation = asyncio.create_task(self._turn())
            try:
                await self.operation
            except asyncio.CancelledError:
                if self.closed:
                    break
            except Exception as exc:
                self.last_error = exc
                logger.exception("Agent session failed: %s", self.session_id)
            finally:
                self.operation = None
                if not self.changed.is_set():
                    self.idle.set()
                    self._state(False)

    async def _turn(self) -> None:
        started = time.monotonic()
        turn_id = uuid4().hex
        outcome, error = "completed", None
        continue_tools = False
        try:
            for _ in range(self.max_steps):
                selection = await self._prepare(continue_tools=continue_tools)
                if selection is None:
                    break
                self.changed.clear()
                try:
                    reply = await self.generate(
                        selection.operation_id, through_seq=selection.through_seq
                    )
                finally:
                    await self.store.advance(self.session_id, selection.input_seq)
                calls = await self.store.tool_calls(self.session_id, reply.record_id)
                if calls:
                    await self._execute(reply, calls)
                continue_tools = bool(calls)
            else:
                outcome = "limit"
        except asyncio.CancelledError:
            outcome = "cancelled"
            raise
        except Exception as exc:
            outcome, error = "failed", type(exc).__name__
            await self.append(
                [
                    self.make(
                        m.NoticeRecord,
                        payload={
                            "level": "error",
                            "content": [
                                m.TextPart(
                                    text=f"Agent operation failed: {error}: {exc}"
                                )
                            ],
                        },
                    )
                ]
            )
            raise
        finally:
            await self.append(
                [
                    self.make(
                        m.TurnEndRecord,
                        payload={
                            "turn_id": turn_id,
                            "outcome": outcome,
                            "duration_ms": int((time.monotonic() - started) * 1000),
                            "error": error,
                        },
                    )
                ]
            )

    async def generate(
        self,
        operation_id: str | None,
        *,
        through_seq: int | None = None,
        service: bool = False,
        attempt: int = 1,
        previous_attempt: str | None = None,
    ) -> m.AssistantMessageRecord | m.RequestOutputRecord:
        draft = await self.store.openai(
            self.session_id, operation_id, through_seq=through_seq
        )
        if draft.provider is None:
            raise RuntimeError("Validated request has no provider")

        through_seq = draft.validation_data.selection.seq
        context = AttemptContext(
            attempt,
            uuid4().hex,
            uuid4().hex,
            uuid4().hex,
            previous_attempt,
            service,
            datetime.now(UTC),
            time.monotonic(),
        )
        output = Generation()
        failure = None
        reply = None
        try:
            await self.llm.generate(
                draft.provider, draft.request, output, partial(self._emit, context)
            )
            if service:
                if output.calls():
                    raise ValueError("Service inference returned tool calls")
        except BaseException as exc:
            failure = exc
            if output.status is None:
                output.status = getattr(exc, "status", None)
        finally:
            reply = await self._save_generation(context, output, failure)

        if failure is not None:
            delay = self._retry_delay(output, failure, attempt)
            if delay is None:
                raise failure

            await asyncio.sleep(delay)
            return await self.generate(
                operation_id,
                through_seq=through_seq,
                service=service,
                attempt=attempt + 1,
                previous_attempt=context.operation_id,
            )

        if reply is None:
            raise ValueError("Inference returned no message")

        return reply

    def _retry_delay(
        self, output: Generation, failure: BaseException, attempt: int
    ) -> float | None:
        if output.has_content:
            return None

        retry = getattr(self.llm, "retry_delay", None)
        if retry is None:
            return None

        return retry(output, failure, attempt)

    async def _emit(
        self, context: AttemptContext, position: int, kind: str, text: str
    ) -> None:
        if context.service:
            return

        channel: Literal["text", "reasoning"] = "text"
        if kind == "reasoning":
            channel = "reasoning"

        delta = TextDelta(context.output_id, position, channel, text)
        async with self.lock:
            for protocol in self._protocols().values():
                self._publish([protocol.chunk(self.session_id, delta)], protocol)

            accumulated = text
            if previous := self.partial.get(position):
                accumulated = previous.text + text

            self.partial[position] = TextDelta(
                context.output_id, position, channel, accumulated
            )

    async def _save_generation(
        self, context: AttemptContext, output: Generation, failure: BaseException | None
    ) -> m.AssistantMessageRecord | m.RequestOutputRecord | None:
        records: list[m.HistoryRecord] = []
        attempt = self.records.attempt(context, output, failure)
        if attempt is not None:
            records.append(attempt)
            records.extend(self.records.response(context, output, failure))

        reply = None
        async with self.lock:
            if records:
                saved = await self.store.append(self.session_id, records)
                for row in saved:
                    if isinstance(
                        row, (m.AssistantMessageRecord, m.RequestOutputRecord)
                    ):
                        reply = row

                await self._publish_saved(saved)

            if not context.service:
                self.partial.clear()

        return reply

    async def _execute(self, reply, calls: Sequence[m.ToolCallRecord]) -> None:
        remaining = list(calls)
        try:
            for call in calls:
                try:
                    content = await self.tools.call(self, reply, call)
                    if isinstance(content, m.ToolResultRecord):
                        remaining.pop(0)
                        continue

                    status = "completed"
                except Exception as exc:
                    content = getattr(exc, "content", None)
                    if content is None:
                        content = (
                            m.TextPart(
                                text=f"Tool {call.payload.name}, call {call.key} failed: {exc}"
                            ),
                        )
                    status = "failed"

                await self.append(
                    [
                        self.make(
                            m.ToolResultRecord,
                            key=call.key,
                            payload={
                                "call_record_id": call.record_id,
                                "status": status,
                                "content": content,
                            },
                        )
                    ]
                )
                remaining.pop(0)
        finally:
            for call in remaining:
                await self.append(
                    [
                        self.make(
                            m.ToolResultRecord,
                            key=call.key,
                            payload={
                                "call_record_id": call.record_id,
                                "status": "interrupted",
                                "content": [
                                    m.TextPart(
                                        text="Execution interrupted; completion is not confirmed."
                                    )
                                ],
                            },
                        )
                    ]
                )

    async def service_request(
        self, inputs: Sequence[m.HistoryRecord], operation_id: str
    ):
        for item in inputs:
            if item.operation_id != operation_id:
                raise ValueError(
                    "Service inputs must belong to the selected operation_id"
                )

        values = list(inputs)
        values.append(
            self.make(
                m.StreamingRecord,
                operation_id=operation_id,
                payload={"value": {"stream": False}},
            )
        )
        records = self.records.group(values, operation_id)
        saved = await self.append(records)
        return await self.generate(
            operation_id, through_seq=saved[-1].seq, service=True
        )

    async def cancel(self) -> None:
        if self.operation is not None:
            self.operation.cancel()
            with suppress(asyncio.CancelledError):
                await self.operation

    async def task(
        self, reply, call, action, *, background: bool, child_session_id=None
    ):
        """Одинаковое исполнение вызова; background меняет только ожидание результата."""
        task_id = uuid4().hex

        revision = 0

        async def save(status, *, content=(), error=None):
            nonlocal revision
            revision += 1
            result = None
            if status == "completed":
                result = [part.model_dump(mode="json") for part in content]

            if not content:
                content = (
                    m.TextPart(text=f"Task {task_id} ({call.payload.name}): {status}"),
                )

            state = self.make(
                m.ToolResultRecord,
                key=call.key,
                payload={
                    "call_record_id": call.record_id,
                    "status": status,
                    "state": {
                        "task_id": task_id,
                        "revision": revision,
                        "description": call.payload.name,
                        "status": status,
                        "child_session_id": child_session_id,
                        "result": result,
                        "error": error,
                    },
                    "content": content,
                },
            )
            saved = await self.append([state])
            return m.ToolResultRecord.model_validate(saved[0])

        async def run():
            try:
                result = await action(task_id)
            except asyncio.CancelledError:
                await save("cancelled")
                raise
            except Exception as exc:
                return await save("failed", error=f"{type(exc).__name__}: {exc}")
            else:
                return await save("completed", content=result)
            finally:
                if background and not self.closed:
                    self.wake(continue_tools=True)

        started = await save("running")
        if not background:
            return await run()

        running = asyncio.create_task(run(), name=f"task:{task_id}")
        self.background.add(running)
        running.add_done_callback(self.background.discard)
        return started

    async def close(self) -> None:
        self.closed = True
        await self.cancel()
        for task in tuple(self.background):
            task.cancel()
        if self.background:
            await asyncio.gather(*tuple(self.background), return_exceptions=True)
        if self.worker is not None:
            self.worker.cancel()
            with suppress(asyncio.CancelledError):
                await self.worker
        for subscriber in tuple(self.subscribers):
            await subscriber.close()

        self.subscribers.clear()
