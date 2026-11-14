"""Исполнение сессии: сохранение, принятие ввода, SQL-запрос, LLM и tools."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from abc import abstractmethod
from collections.abc import Awaitable, Callable, Sequence
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal, Protocol, TypeVar
from uuid import uuid4

from boba.agent import models as m
from boba.agent.history import HistoryStore
from boba.agent.llm import Generation
from boba.agent.queries import Query

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
    async def options(self, session_id: str) -> tuple[str, dict]: ...

    @abstractmethod
    def chunk(self, session_id: str, delta: TextDelta) -> tuple[str, dict]: ...

    @abstractmethod
    def state(self, session_id: str, running: bool) -> list[tuple[str, dict]]: ...

    @abstractmethod
    async def request_action(
        self, session_id: str, record: m.HistoryRecord
    ) -> dict: ...


def record(model: type[R], version: str, *, payload: Any = None, **fields: Any) -> R:
    """Производитель задаёт данные; seq назначает только INSERT в PostgreSQL."""
    return model.model_validate(
        {
            "record_id": fields.pop("record_id", uuid4().hex),
            "created_at": datetime.now(UTC),
            "version": version,
            "payload": {} if payload is None else payload,
            **fields,
        }
    )


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

    def make(self, model: type[R], **fields) -> R:
        return record(model, self.version, **fields)

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
            if isinstance(item, m.NoticeRecord):
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
            update = await protocol.options(self.session_id)
            self._publish([update], protocol)

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
        self, content: Sequence[m.UserPart], *, record_id: str | None = None
    ) -> str:
        if self.closed:
            raise RuntimeError("Agent is closed")
        fields = {} if record_id is None else {"record_id": record_id}
        value = self.make(m.UserMessageRecord, payload={"content": content}, **fields)
        await self.append([value])
        self.wake()
        return value.record_id

    def wake(self) -> None:
        self.idle.clear()
        self.changed.set()
        if self.worker is None:
            self.worker = asyncio.create_task(
                self._run(), name=f"agent:{self.session_id}"
            )

    async def _prepare(self, *, continue_tools: bool) -> str | None:
        # The advisory lock protects acceptance against concurrent writers.
        async with self.lock:
            async with self.store.transaction(self.session_id) as transaction:
                pending = await transaction.pending_inputs()
                tasks = await transaction.rows(Query.PENDING_TASKS, self.session_id)
                if not pending and not tasks and not continue_tools:
                    return None
                request_id = uuid4().hex
                records: list[m.HistoryRecord] = [
                    self.make(
                        m.MessageAcceptedRecord,
                        payload={"source_record_id": item.record_id},
                    )
                    for item in pending
                ]
                for task in tasks:
                    state = m.TaskStateRecord.model_validate(task["record"])
                    records.append(
                        self.make(
                            m.TaskContextRecord,
                            key=state.payload.task_id,
                            payload={
                                "related_message_id": state.record_id,
                                "state": state.payload.state,
                                "content": [
                                    m.TextPart(
                                        text=f"Task {state.payload.task_id}: {state.payload.state.status}"
                                    )
                                ],
                            },
                        )
                    )
                    if state.payload.state.status in {
                        "completed",
                        "failed",
                        "cancelled",
                    }:
                        records.append(
                            self.make(
                                m.TaskNotificationRecord,
                                payload={
                                    "task_id": state.payload.task_id,
                                    "status": state.payload.state.status,
                                    "related_message_id": state.record_id,
                                    "level": state.payload.level,
                                    "content": state.payload.content,
                                },
                            )
                        )
                records.extend(
                    [
                        self.make(
                            m.StreamingRecord,
                            request_id=request_id,
                            payload={
                                "value": {
                                    "stream": True,
                                    "stream_options": {"include_usage": True},
                                }
                            },
                        ),
                        self.make(m.LlmRequestBoundaryRecord, record_id=request_id),
                    ]
                )
                saved = await transaction.append(records)
            await self._publish_saved(saved)
        return request_id

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
                request_id = await self._prepare(continue_tools=continue_tools)
                if request_id is None:
                    break
                self.changed.clear()
                reply = await self.generate(request_id)
                acknowledgements = await self.store.rows(
                    Query.TASK_ACKNOWLEDGEMENTS, self.session_id, request_id
                )
                if acknowledgements:
                    await self.append(
                        [
                            self.make(
                                m.TaskAcknowledgedRecord,
                                key=row["task_id"],
                                payload={
                                    "notification_id": row["notification_id"],
                                    "related_message_id": reply.record_id,
                                },
                            )
                            for row in acknowledgements
                        ]
                    )
                calls = [
                    part
                    for part in reply.payload.content
                    if isinstance(part, m.ToolCallPart)
                ]
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
                        m.TextNoticeRecord,
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
        self, request_id: str, *, service: bool = False, attempt: int = 1
    ):
        draft = await self.store.openai(self.session_id, request_id)
        if draft.provider is None:
            raise RuntimeError("Validated request has no provider")
        output_id = uuid4().hex
        output = Generation()

        async def emit(position: int, kind: str, text: str) -> None:
            if service:
                return

            channel: Literal["text", "reasoning"] = "text"
            if kind == "reasoning":
                channel = "reasoning"

            delta = TextDelta(output_id, position, channel, text)
            async with self.lock:
                for protocol in self._protocols().values():
                    frame = protocol.chunk(self.session_id, delta)
                    self._publish([frame], protocol)

                accumulated = text
                if previous := self.partial.get(position):
                    accumulated = previous.text + text

                self.partial[position] = TextDelta(
                    output_id, position, channel, accumulated
                )

        started = time.monotonic()
        error = None
        failure = None
        completion = "complete"
        content = ()
        try:
            await self.llm.generate(draft.provider, draft.request, output, emit)
            if output.finish_reason == "length":
                completion = "output_limit"
            content = output.content(partial=completion != "complete")
        except BaseException as exc:
            error = type(exc).__name__
            failure = exc
            if output.status is None:
                output.status = getattr(exc, "status", None)
            completion = "cancelled"
            content = output.content(partial=True)
        finally:
            records = []
            reply = None
            if content:
                reply = self.make(
                    m.RequestOutputRecord if service else m.AssistantMessageRecord,
                    record_id=output_id,
                    request_id=request_id,
                    payload={"content": content, "completion": completion},
                )
                records.append(reply)
            if output.usage is not None:
                records.append(
                    self.make(
                        m.TokenUsageRecord,
                        request_id=request_id,
                        payload={"usage": output.usage},
                    )
                )
            records.append(
                self.make(
                    m.LlmRequestAttemptRecord,
                    request_id=request_id,
                    payload={
                        "attempt": attempt,
                        "method": "POST",
                        "url": self.llm.address(draft.provider),
                        "status_code": output.status,
                        "duration_ms": int((time.monotonic() - started) * 1000),
                        "error": error,
                        "level": "warning" if error else "info",
                        "content": [
                            m.TextPart(
                                text=f"LLM request {request_id}: {error or 'completed'}"
                            )
                        ],
                    },
                )
            )
            async with self.lock:
                saved = await self.store.append(self.session_id, records)
                if reply is not None:
                    reply = next(
                        row
                        for row in saved
                        if isinstance(row, m.AssistantMessageRecord)
                        and row.record_id == output_id
                    )
                await self._publish_saved(saved)
                if not service:
                    self.partial.clear()
        if failure is not None:
            retry = getattr(self.llm, "retry_delay", None)
            if not output.has_content and retry is not None:
                delay = retry(draft.provider, failure, attempt)
                if delay is not None:
                    await asyncio.sleep(delay)
                    return await self.generate(
                        request_id, service=service, attempt=attempt + 1
                    )
            raise failure
        if reply is None:
            raise ValueError("Inference returned no message")
        return reply

    async def _execute(self, reply, calls: list[m.ToolCallPart]) -> None:
        # Cancellation produces real execution outcomes, never a repair inside assembly.
        remaining = list(calls)
        try:
            for call in calls:
                try:
                    content = await self.tools.call(self, reply, call)
                    result = {
                        "call_id": call.call_id,
                        "content": content,
                        "is_error": False,
                    }
                except Exception as exc:
                    error_content = getattr(exc, "content", None)
                    if error_content is None:
                        error_content = (
                            m.TextPart(
                                text=f"Tool {call.name}, call {call.call_id} failed: {exc}"
                            ),
                        )

                    result = {
                        "call_id": call.call_id,
                        "content": error_content,
                        "is_error": True,
                    }

                await self.append([self.make(m.ToolResultRecord, payload=result)])
                remaining.pop(0)
        finally:
            if remaining:
                await self.append(
                    [
                        self.make(
                            m.ToolResultRecord,
                            payload={
                                "call_id": call.call_id,
                                "is_error": True,
                                "content": [
                                    m.TextPart(
                                        text="Execution cancelled; completion is not confirmed."
                                    )
                                ],
                            },
                        )
                        for call in remaining
                    ]
                )

    async def service_request(self, inputs: Sequence[m.HistoryRecord], request_id: str):
        if any(item.request_id != request_id for item in inputs):
            raise ValueError("Service inputs must reference the same request_id")
        await self.append(
            [*inputs, self.make(m.LlmRequestBoundaryRecord, record_id=request_id)]
        )
        return await self.generate(request_id, service=True)

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
        mode = "background" if background else "foreground"

        async def save(status, *, result=None, error=None):
            text = f"Task {task_id} ({call.name}): {status}"
            if result is not None:
                text += "\n" + json.dumps(result, ensure_ascii=False)
            state = self.make(
                m.TaskStateRecord,
                payload={
                    "task_id": task_id,
                    "related_message_id": reply.record_id,
                    "call_id": call.call_id,
                    "level": "error" if error else "info",
                    "content": [m.TextPart(text=text)],
                    "state": {
                        "execution_mode": mode,
                        "description": call.name,
                        "status": status,
                        "child_session_id": child_session_id,
                        "result": result,
                        "error": error,
                    },
                },
            )
            await self.append([state])

        async def run():
            try:
                result = await action(task_id)
            except asyncio.CancelledError:
                await save("cancelled")
                raise
            except Exception as exc:
                await save("failed", error=type(exc).__name__)
                if not background:
                    raise
                return ()
            else:
                await save(
                    "completed",
                    result=[part.model_dump(mode="json") for part in result],
                )
                return result
            finally:
                if background and not self.closed:
                    self.wake()

        await save("running")
        if not background:
            return await run()
        running = asyncio.create_task(run(), name=f"task:{task_id}")
        self.background.add(running)
        running.add_done_callback(self.background.discard)
        return (m.TextPart(text=f"Started background task {task_id}"),)

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
