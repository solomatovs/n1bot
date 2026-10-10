"""Цикл хода: от взятого элемента очереди до ответа модели без вызовов.

Порядок шага — docs/prompt_assembly/specs/03_request_pipeline.md, B.1, и
план, раздел 5.13: вложения состояния, объявления контекста, запрос, записи
ответа, повторы при ошибках модели, обрыв по пределу длины, отмена
(раздел 5.14). Исполнение инструментов и сжатие подключаются следующими
этапами; вызовы в ответе пока записываются и завершают ход.

Ошибки:
SessionError — нарушен порядок записей сессии (ошибка ядра).
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import ClassVar

from boba.agent.attachments import (
    Announced,
    ContextAttachments,
    PromptSnapshot,
    SnapshotTool,
)
from boba.agent.builder import RequestBuilder
from boba.agent.events import (
    EventSink,
    RecordEvent,
    ResultEvent,
    StreamEvent,
    TurnOutcome,
)
from boba.agent.ids import Clock, IdMint
from boba.agent.profile import ModelProfile, TurnLimits
from boba.agent.queue import QueueKind, Taken
from boba.agent.records import (
    ApiErrorRecord,
    AssistantBlock,
    AssistantMessage,
    AssistantRecord,
    MessageOrigin,
    OriginKind,
    Record,
    ReplyStopReason,
    Synthetic,
    TextBlock,
    ThinkingBlock,
    ToolUseBlock,
    Usage,
    UserMessage,
    UserRecord,
)
from boba.agent.session import Session
from boba.agent.template import AgentTemplate, AgentTemplateFile
from boba.agent.writer import HistoryWriter
from boba.cancellation import RunCancellation, StopReason, ToolStopped
from boba.llm.chat import (
    ChatDelta,
    ChatModel,
    ChatReply,
    ChatRequest,
    ChatUsage,
    LlmContextOverflowError,
    LlmError,
    LlmOutputLimitError,
)

logger = logging.getLogger(__name__)

__all__ = ["TurnLoop"]


@dataclass
class Partial:
    """Текст и рассуждения, пришедшие кусками до обрыва или конца потока."""

    content: list[str] = field(default_factory=list)
    reasoning: list[str] = field(default_factory=list)

    def take(self, delta: ChatDelta) -> None:
        if delta.content:
            self.content.append(delta.content)

        if delta.reasoning:
            self.reasoning.append(delta.reasoning)

    def is_empty(self) -> bool:
        return not self.content and not self.reasoning

    def as_reply(self) -> ChatReply:
        return ChatReply(
            content="".join(self.content), reasoning="".join(self.reasoning)
        )


@dataclass(frozen=True)
class Answered:
    reply: ChatReply


@dataclass(frozen=True)
class OutputLimited:
    partial: Partial
    error: str


@dataclass(frozen=True)
class Overflowed:
    error: str


@dataclass(frozen=True)
class Failed:
    error: str


@dataclass(frozen=True)
class Aborted:
    partial: Partial
    reason: StopReason


Asked = Answered | OutputLimited | Overflowed | Failed | Aborted


class TurnLoop:
    """Ведёт ход: записи, запросы, повторы, исход."""

    MILLIS: ClassVar[int] = 1000

    def __init__(  # noqa: PLR0913 — цикл хода собирается всеми частями ядра
        self,
        session: Session,
        profile: ModelProfile,
        limits: TurnLimits,
        model: ChatModel,
        builder: RequestBuilder,
        attachments: ContextAttachments,
        templates: AgentTemplate,
        ids: IdMint,
        clock: Clock,
        version: str,
        sink: EventSink,
        writer: HistoryWriter,
        system_prompt: Sequence[str],
        tools: Sequence[SnapshotTool],
    ) -> None:
        self._session = session
        self._profile = profile
        self._limits = limits
        self._model = model
        self._builder = builder
        self._attachments = attachments
        self._templates = templates
        self._ids = ids
        self._clock = clock
        self._version = version
        self._sink = sink
        self._writer = writer
        self._system_prompt = list(system_prompt)
        self._tools = list(tools)
        self._last_failure = ""
        self._failures = 0

    async def run(self, taken: Taken, cancellation: RunCancellation) -> TurnOutcome:
        started = time.monotonic()
        self._failures = 0
        usage_total = Usage()
        steps = 0
        limit_hits = 0

        await self._write_prompt(taken)
        await self._announce_model()

        outcome = TurnOutcome.COMPLETED
        while True:
            steps += 1
            if steps > self._limits.max_turns:
                outcome = TurnOutcome.MAX_TURNS
                break

            snapshot = await self._announce_context()
            request = self._builder.build(self._session.conversation(), snapshot)
            asked = await self._ask(request, cancellation)

            if isinstance(asked, Answered):
                usage_total = self._summed(usage_total, asked.reply.usage)
                await self._write_reply(asked.reply, self._stop_reason(asked.reply))
                if asked.reply.tool_calls:
                    logger.warning(
                        "turn: the model called %d tool(s); tools are not wired yet",
                        len(asked.reply.tool_calls),
                    )

                outcome = TurnOutcome.COMPLETED
                break

            if isinstance(asked, OutputLimited):
                limit_hits += 1
                await self._write_partial(
                    asked.partial, ReplyStopReason.MAX_TOKENS, False
                )
                if limit_hits > self._limits.output_limit_retries:
                    await self._write_api_failure(asked.error)
                    outcome = TurnOutcome.API_ERROR
                    break

                await self._write_meta(
                    self._templates.read(AgentTemplateFile.OUTPUT_LIMIT_HIT)
                )
                continue

            if isinstance(asked, Overflowed):
                await self._write_api_error(asked.error, 0, 0)
                await self._write_api_failure(
                    self._templates.read(AgentTemplateFile.PROMPT_TOO_LONG)
                )
                outcome = TurnOutcome.BLOCKING_LIMIT
                break

            if isinstance(asked, Failed):
                await self._write_api_failure(asked.error)
                outcome = TurnOutcome.API_ERROR
                break

            outcome = await self._write_abort(asked)
            break

        duration = int((time.monotonic() - started) * self.MILLIS)
        await self._sink.emit(
            ResultEvent(
                outcome=outcome,
                session_id=self._session.id.value,
                usage=usage_total,
                num_turns=steps,
                duration_ms=duration,
            )
        )
        await self._writer.flush()

        return outcome

    async def _ask(self, request: ChatRequest, cancellation: RunCancellation) -> Asked:
        """Запрос с повторами: три попытки на `LlmError`, пауза между ними."""
        attempt = 0
        while True:
            asked = await self._attempt(request, cancellation)
            if asked is not None:
                return asked

            attempt += 1
            if attempt > self._limits.api_retries:
                return Failed(self._last_failure)

            await asyncio.sleep(self._limits.delay_before(attempt))

    async def _attempt(
        self, request: ChatRequest, cancellation: RunCancellation
    ) -> Asked | None:
        """Одна попытка; None — ошибка, которую стоит повторить."""
        partial = Partial()
        try:
            reply = await self._stream(request, partial, cancellation)
        except (ToolStopped, asyncio.CancelledError) as exc:
            return self._aborted(exc, partial, cancellation)
        except LlmContextOverflowError as exc:
            return Overflowed(str(exc))
        except LlmOutputLimitError as exc:
            return OutputLimited(partial, str(exc))
        except LlmError as exc:
            self._last_failure = str(exc)
            self._failures += 1
            await self._write_api_error(
                self._last_failure, self._failures, self._limits.api_retries
            )
            return None

        if self._is_empty(reply):
            self._last_failure = "the model returned an empty reply"
            logger.warning("turn: the model replied with nothing to record, retrying")
            return None

        return Answered(reply)

    async def _stream(
        self, request: ChatRequest, partial: Partial, cancellation: RunCancellation
    ) -> ChatReply:
        task = asyncio.current_task()
        if task is None:
            msg = "The turn loop was called outside an asyncio task. This is a bug."
            raise RuntimeError(msg)

        reply: ChatReply | None = None
        with cancellation.abort_with(self._canceller(task)):
            async for event in self._model.chat(request):
                if isinstance(event, ChatReply):
                    reply = event
                    continue

                partial.take(event)
                await self._sink.emit(
                    StreamEvent(content=event.content, reasoning=event.reasoning)
                )

        if reply is None:
            msg = (
                "The model stream ended without a final reply: the provider closed "
                "the connection before the answer was complete."
            )
            raise LlmError(msg)

        return reply

    def _aborted(
        self, exc: BaseException, partial: Partial, cancellation: RunCancellation
    ) -> Aborted:
        """Отмена хода; чужая отмена задачи идёт дальше."""
        if isinstance(exc, asyncio.CancelledError):
            if not cancellation.cancelled:
                raise exc

            self._uncancel()

        return Aborted(partial, self._reason(cancellation))

    def _canceller(self, task: asyncio.Task[object]) -> Callable[[], None]:
        def cancel() -> None:
            task.cancel()

        return cancel

    def _uncancel(self) -> None:
        task = asyncio.current_task()
        if task is not None:
            task.uncancel()

    def _reason(self, cancellation: RunCancellation) -> StopReason:
        reason = cancellation.reason
        if reason is None:
            return StopReason.USER_STOP

        return reason

    def _is_empty(self, reply: ChatReply) -> bool:
        if reply.tool_calls:
            return False

        return not reply.content.strip()

    def _stop_reason(self, reply: ChatReply) -> ReplyStopReason:
        if reply.tool_calls:
            return ReplyStopReason.TOOL_USE

        return ReplyStopReason.END_TURN

    def _summed(self, total: Usage, usage: ChatUsage) -> Usage:
        return Usage(
            input_tokens=total.input_tokens + usage.input_tokens,
            output_tokens=total.output_tokens + usage.output_tokens,
        )

    async def _announce_model(self) -> None:
        announced = Announced(self._session.conversation())
        if announced.model_id() == self._profile.model_id:
            return

        record = self._attachments.model(self._session, self._profile)
        await self._emitted(record)

    async def _announce_context(self) -> PromptSnapshot:
        """Дата, если сменилась; снимок, если его нет в доступной истории."""
        announced = Announced(self._session.conversation())
        today = self._attachments.today()
        known = announced.date()
        if known != today:
            record = self._attachments.date(self._session, today, known is not None)
            await self._emitted(record)

        snapshot = announced.snapshot()
        if snapshot is None:
            snapshot = PromptSnapshot(
                system_prompt=self._system_prompt,
                tools=self._tools,
                model=self._profile.model_id,
                system_turns=self._profile.system_turns,
            )
            record = self._attachments.snapshot(self._session, snapshot)
            await self._emitted(record)
            return snapshot

        if snapshot.system_prompt != self._system_prompt:
            logger.warning(
                "turn: the system prompt of the launch differs from the snapshot in "
                "history; the request follows the snapshot"
            )

        return snapshot

    async def _write_prompt(self, taken: Taken) -> None:
        origin = MessageOrigin(kind=OriginKind.HUMAN)
        is_meta: bool | None = None
        if taken.head.kind is QueueKind.TASK_NOTIFICATION:
            origin = MessageOrigin(kind=OriginKind.TASK_NOTIFICATION)
            is_meta = True

        record = UserRecord(
            uuid=self._ids.uuid(),
            parent_uuid=self._session.last_chain_uuid(),
            timestamp=self._clock.now(),
            session_id=self._session.id.value,
            version=self._version,
            message=UserMessage(content=taken.text),
            is_meta=is_meta,
            origin=origin,
        )
        await self._emitted(self._session.add(record))

    async def _write_meta(self, text: str) -> None:
        record = UserRecord(
            uuid=self._ids.uuid(),
            parent_uuid=self._session.last_chain_uuid(),
            timestamp=self._clock.now(),
            session_id=self._session.id.value,
            version=self._version,
            message=UserMessage(content=text),
            is_meta=True,
        )
        await self._emitted(self._session.add(record))

    async def _write_reply(
        self, reply: ChatReply, stop_reason: ReplyStopReason, aborted: bool = False
    ) -> None:
        """Ответ модели — по записи на блок с общими `message.id`, `usage`."""
        blocks: list[tuple[AssistantBlock, str | None]] = []
        if reply.reasoning:
            blocks.append((ThinkingBlock(thinking=reply.reasoning), None))

        if reply.content:
            blocks.append((TextBlock(text=reply.content), None))

        for call in reply.tool_calls:
            block = ToolUseBlock(
                id=self._ids.call_id(), name=call.name, input=dict(call.arguments)
            )
            blocks.append((block, call.id))

        message_id = self._ids.message_id()
        request_id = self._ids.request_id()
        usage = Usage(
            input_tokens=reply.usage.input_tokens,
            output_tokens=reply.usage.output_tokens,
        )
        mid_stream: bool | None = None
        if aborted:
            mid_stream = True

        for index, (block, wire_id) in enumerate(blocks):
            record = AssistantRecord(
                uuid=self._ids.uuid(),
                parent_uuid=self._session.last_chain_uuid(),
                timestamp=self._clock.now(),
                session_id=self._session.id.value,
                version=self._version,
                message=AssistantMessage(
                    id=message_id,
                    model=self._profile.model_id,
                    content=[block],
                    stop_reason=stop_reason,
                    usage=usage,
                ),
                api_block_index=index,
                request_id=request_id,
                is_aborted_mid_stream=mid_stream,
                wire_tool_call_id=wire_id,
            )
            await self._emitted(self._session.add(record))

    async def _write_partial(
        self, partial: Partial, stop_reason: ReplyStopReason, aborted: bool
    ) -> None:
        if partial.is_empty():
            return

        await self._write_reply(partial.as_reply(), stop_reason, aborted)

    async def _write_api_error(self, error: str, attempt: int, retries: int) -> None:
        record = ApiErrorRecord(
            uuid=self._ids.uuid(),
            parent_uuid=self._session.last_chain_uuid(),
            timestamp=self._clock.now(),
            session_id=self._session.id.value,
            version=self._version,
            error=error,
            retry_attempt=attempt,
            max_retries=retries,
        )
        await self._emitted(self._session.add(record))

    async def _write_api_failure(self, error: str) -> None:
        """Синтетическая запись об ошибке модели; модели не отправляется."""
        record = AssistantRecord(
            uuid=self._ids.uuid(),
            parent_uuid=self._session.last_chain_uuid(),
            timestamp=self._clock.now(),
            session_id=self._session.id.value,
            version=self._version,
            message=AssistantMessage(
                id=self._ids.message_id(),
                model=Synthetic.MODEL.value,
                content=[TextBlock(text=error)],
                stop_reason=ReplyStopReason.STOP_SEQUENCE,
                usage=Usage(),
            ),
            request_id=self._ids.request_id(),
            is_api_error_message=True,
            error=error,
        )
        await self._emitted(self._session.add(record))

    async def _write_abort(self, aborted: Aborted) -> TurnOutcome:
        """Записи отмены по таблице раздела 5.14 для стадии «ответ идёт»."""
        await self._write_partial(aborted.partial, ReplyStopReason.END_TURN, True)
        if aborted.reason is StopReason.SUPERSEDED:
            return TurnOutcome.ABORTED_STREAMING

        by_shutdown: bool | None = None
        outcome = TurnOutcome.ABORTED_STREAMING
        if aborted.reason is StopReason.SHUTDOWN:
            by_shutdown = True
            outcome = TurnOutcome.SHUTDOWN

        record = UserRecord(
            uuid=self._ids.uuid(),
            parent_uuid=self._session.last_chain_uuid(),
            timestamp=self._clock.now(),
            session_id=self._session.id.value,
            version=self._version,
            message=UserMessage(
                content=self._templates.read(AgentTemplateFile.REQUEST_INTERRUPTED)
            ),
            interrupted_by_shutdown=by_shutdown,
        )
        await self._emitted(self._session.add(record))

        return outcome

    async def _emitted(self, record: Record) -> None:
        """Записанная запись: разбудить писателя и показать наружу записи разговора."""
        self._writer.notify()
        if isinstance(record, UserRecord | AssistantRecord):
            await self._sink.emit(RecordEvent(record=record))
