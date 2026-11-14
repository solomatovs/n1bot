"""Исполнение сессии: сохранение, принятие ввода, SQL-запрос, LLM и tools."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable, Iterable, Iterator, Sequence
from contextlib import suppress
from datetime import UTC, datetime
from typing import Any, Literal, TypeVar
from uuid import uuid4

from acp.experimental.v2 import schema

from boba.agent import models as m
from boba.agent.acp_validator import TextMessageChunk
from boba.agent.history import HistoryStore
from boba.agent.llm import Generation
from boba.agent.queries import Query

logger = logging.getLogger(__name__)
Send = Callable[[str, dict[str, Any]], Awaitable[None]]
R = TypeVar("R", bound=m.RecordBase)


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
        disconnect=None,
    ) -> None:
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
                await send(method, params)
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
        self.partial: dict[int, TextMessageChunk] = {}
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

    async def _publish_saved(self, saved) -> None:
        draft = await self.store.acp(
            Query.ACP_HISTORY, self.session_id, saved[0].seq - 1, saved[-1].seq
        )
        for item in draft["notifications"]:
            notification = item["notification"]
            self._publish(notification["method"], notification["params"])
        for item in saved:
            if isinstance(item, m.NoticeRecord):
                notice = await self.store.acp(
                    Query.ACP_NOTICE, self.session_id, item.record_id
                )
                self.update(notice["update"])

    def update(self, update: dict) -> None:
        self._publish(
            "session/update", {"sessionId": self.session_id, "update": update}
        )

    def _publish(self, method: str, params: dict) -> None:
        for subscriber in tuple(self.subscribers):
            subscriber.publish(method, params)
            if subscriber.failed:
                self.subscribers.discard(subscriber)

    async def subscribe(
        self,
        send: Send,
        *,
        replay_history: bool = True,
        after_seq: int = 0,
        disconnect=None,
    ) -> Subscription:
        async with self.lock:
            head = await self.store.head(self.session_id)
            state = await self.store.acp(Query.ACP_STATE, self.session_id, head)
            commands = await self.store.acp(Query.ACP_COMMANDS, self.session_id, head)
            options = await self.store.acp(Query.ACP_OPTIONS, self.session_id, head)
            initial: list[tuple[str, dict]] = []
            if replay_history:
                history = await self.store.acp(
                    Query.ACP_HISTORY, self.session_id, after_seq, head
                )
                initial.extend(self._history_notifications(history))
                end = (state["replay_complete"],)
                initial.extend(self._session_updates(end))

            foreground = "running"
            if self.idle.is_set():
                foreground = "idle"

            updates = (
                state["update"],
                commands["update"],
                options["update"],
                {
                    "sessionUpdate": "state_update",
                    "state": foreground,
                },
            )
            initial.extend(self._session_updates(updates))
            initial.extend(self._session_updates(self._partial_updates()))
            subscriber = Subscription(send, initial, self.subscriber_buffer, disconnect)
            self.subscribers.add(subscriber)

        try:
            await asyncio.shield(subscriber.ready)

        except BaseException:
            self.subscribers.discard(subscriber)
            await subscriber.close()
            raise

        return subscriber

    def _history_notifications(self, history: dict) -> Iterator[tuple[str, dict]]:
        for row in history["notifications"]:
            notification = row["notification"]
            yield notification["method"], notification["params"]

    def _session_updates(self, updates: Iterable[dict]) -> Iterator[tuple[str, dict]]:
        for update in updates:
            yield "session/update", {"sessionId": self.session_id, "update": update}

    def _partial_updates(self) -> Iterator[dict]:
        for chunk in self.partial.values():
            yield chunk.model_dump(mode="json", by_alias=True, exclude_none=True)

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
            self.update({"sessionUpdate": "state_update", "state": "running"})
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
                    self.update({"sessionUpdate": "state_update", "state": "idle"})

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
                                m.TextPart(text=f"Agent operation failed: {error}")
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

            channel: Literal["agent_message_chunk", "agent_thought_chunk"] = (
                "agent_message_chunk"
            )
            if kind == "reasoning":
                channel = "agent_thought_chunk"

            chunk = TextMessageChunk(
                sessionUpdate=channel,
                messageId=json.dumps([output_id, [position]], ensure_ascii=False),
                content=schema.TextContentBlock(text=text),
            )
            async with self.lock:
                self.update(
                    chunk.model_dump(mode="json", by_alias=True, exclude_none=True)
                )
                accumulated = text
                if previous := self.partial.get(position):
                    accumulated = previous.content.text + text

                self.partial[position] = TextMessageChunk(
                    sessionUpdate=channel,
                    messageId=chunk.message_id,
                    content=schema.TextContentBlock(text=accumulated),
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
                    result = {
                        "call_id": call.call_id,
                        "content": getattr(
                            exc,
                            "content",
                            (m.TextPart(text=f"Tool failed: {type(exc).__name__}"),),
                        ),
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
