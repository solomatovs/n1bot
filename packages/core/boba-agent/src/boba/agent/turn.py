"""Цикл хода: от взятого элемента очереди до ответа модели без вызовов.

Порядок шага — docs/prompt_assembly/specs/03_request_pipeline.md, B.1, и
план, раздел 5.13: подмешивание очереди, объявления контекста, проверка
заполненности со сжатием и предохранителями (раздел 5.16), запрос, записи
ответа, исполнение вызовов с разрешениями (ToolRunner), записи результатов
в порядке вызовов, повторы при ошибках модели, обрыв по пределу длины,
сжатие по переполнению окна с одним повтором, предел шагов, отмена на всех
стадиях (раздел 5.14).

Ошибки:
SessionError — нарушен порядок записей сессии (ошибка ядра).
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import ClassVar

from boba.agent.asking import (
    Aborted,
    Answered,
    Asked,
    Errored,
    ModelAsk,
    OutputLimited,
    Overflowed,
    Partial,
)
from boba.agent.attachments import (
    Announced,
    ContextAttachments,
    EnvironmentSnapshot,
    PromptSnapshot,
    SkillBudget,
    SnapshotSkill,
    SnapshotTool,
)
from boba.agent.builder import RequestBuilder
from boba.agent.compaction import (
    CompactGate,
    Compactor,
    CompactTracking,
    ContextMeter,
    Fill,
    FillLevel,
    GateDecision,
    NotSummarized,
    Summarizer,
    SummaryAborted,
    TokenEstimator,
)
from boba.agent.events import (
    EventSink,
    ResultEvent,
    TurnOutcome,
)
from boba.agent.ids import Clock, IdMint
from boba.agent.profile import CompactionSettings, ModelProfile, TurnLimits
from boba.agent.queue import InputQueue, QueueKind, Taken
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
    ToolResultBlock,
    ToolUseBlock,
    Usage,
    UserMessage,
    UserRecord,
)
from boba.agent.session import Session, SessionState
from boba.agent.template import AgentTemplate, AgentTemplateFile
from boba.agent.tools import BatchOutcome, CallResult, PlannedCall, ToolRunner
from boba.agent.writer import HistoryWriter, RecordKeeper
from boba.cancellation import RunCancellation, StopReason
from boba.llm.chat import ChatModel, ChatReply, ChatRequest, ChatUsage

logger = logging.getLogger(__name__)

__all__ = ["TurnLoop"]


@dataclass(frozen=True)
class Failed:
    """Повторы запроса исчерпаны."""

    error: str


Settled = Asked | Failed


@dataclass(frozen=True)
class Compacted:
    """Сжатие состоялось: записи пачки уже в сессии."""

    records: Sequence[Record]


CompactResult = Compacted | NotSummarized | SummaryAborted


@dataclass
class Progress:
    """Счётчики хода: шаги, обрывы по длине, суммарный учёт, предохранители сжатия."""

    steps: int = 0
    limit_hits: int = 0
    usage: Usage = field(default_factory=Usage)
    tracking: CompactTracking = field(default_factory=CompactTracking)


class TurnLoop:
    """Ведёт ход: записи, запросы, повторы, сжатие, исход."""

    MILLIS: ClassVar[int] = 1000

    def __init__(  # noqa: PLR0913 — цикл хода собирается всеми частями ядра
        self,
        keeper: RecordKeeper,
        queue: InputQueue,
        profile: ModelProfile,
        limits: TurnLimits,
        compaction: CompactionSettings,
        model: ChatModel,
        builder: RequestBuilder,
        attachments: ContextAttachments,
        tools: ToolRunner,
        templates: AgentTemplate,
        ids: IdMint,
        clock: Clock,
        version: str,
        sink: EventSink,
        writer: HistoryWriter,
        system_prompt: Sequence[str],
        snapshot_tools: Sequence[SnapshotTool],
        skills: Sequence[SnapshotSkill],
        environment: EnvironmentSnapshot | None,
        agent_name: str,
    ) -> None:
        self._keeper = keeper
        self._session: Session = keeper.session
        self._queue = queue
        self._profile = profile
        self._limits = limits
        self._builder = builder
        self._attachments = attachments
        self._tools = tools
        self._templates = templates
        self._ids = ids
        self._clock = clock
        self._version = version
        self._sink = sink
        self._writer = writer
        self._system_prompt = list(system_prompt)
        self._snapshot_tools = list(snapshot_tools)
        self._skills = list(skills)
        self._environment = environment
        self._budget = SkillBudget(profile.context_window, profile.chars_per_token)
        self._agent = agent_name
        self._asking = ModelAsk(model, sink)
        estimator = TokenEstimator(profile)
        self._gate = CompactGate(ContextMeter(estimator), compaction, profile)
        self._summarizer = Summarizer(
            self._asking, builder, templates, estimator, ids, clock, version
        )
        self._compactor = Compactor(
            attachments,
            estimator,
            templates,
            ids,
            clock,
            version,
            profile,
            environment,
            self._skills,
            self._budget,
        )
        self._last_failure = ""
        self._failures = 0

    async def run(self, taken: Taken, cancellation: RunCancellation) -> TurnOutcome:
        started = time.monotonic()
        self._failures = 0
        progress = Progress()

        await self._write_prompt(taken)
        await self._absorb()
        await self._announce_input()

        while True:
            progress.steps += 1
            outcome = await self._step(progress, cancellation)
            if outcome is not None:
                break

        duration = int((time.monotonic() - started) * self.MILLIS)
        await self._sink.emit(
            ResultEvent(
                outcome=outcome,
                session_id=self._session.id.value,
                usage=progress.usage,
                num_turns=progress.steps,
                duration_ms=duration,
            )
        )
        await self._writer.flush()

        return outcome

    async def _step(
        self, progress: Progress, cancellation: RunCancellation
    ) -> TurnOutcome | None:
        """Один шаг хода: заполненность, запрос и его разбор; None — ход
        продолжается."""
        if progress.steps > self._limits.max_turns:
            record = self._attachments.max_turns(
                self._session.last_chain_uuid(),
                self._limits.max_turns,
                progress.steps - 1,
            )
            await self._keeper.add(record)
            return TurnOutcome.MAX_TURNS

        guarded = await self._guard_context(progress, cancellation)
        if guarded is not None:
            return guarded

        snapshot = await self._announce_context()
        request = self._builder.build(self._session.conversation(), snapshot)
        asked = await self._ask(request, cancellation)
        if isinstance(asked, Overflowed):
            recovered = await self._recover_overflow(asked, progress, cancellation)
            if isinstance(recovered, TurnOutcome):
                return recovered

            asked = recovered

        return await self._settle(asked, progress, cancellation)

    async def _settle(
        self, asked: Settled, progress: Progress, cancellation: RunCancellation
    ) -> TurnOutcome | None:
        if isinstance(asked, Answered):
            return await self._answered(asked, progress, cancellation)

        if isinstance(asked, OutputLimited):
            return await self._output_limited(asked, progress)

        if isinstance(asked, Overflowed):
            await self._write_api_error(asked.error, 0, 0)
            await self._write_api_failure(
                self._templates.read(AgentTemplateFile.PROMPT_TOO_LONG)
            )
            return TurnOutcome.BLOCKING_LIMIT

        if isinstance(asked, Failed):
            await self._write_api_failure(asked.error)
            return TurnOutcome.API_ERROR

        if isinstance(asked, Errored):
            msg = "An attempt-level model error reached the settlement. This is a bug."
            raise RuntimeError(msg)

        return await self._write_abort(asked)

    async def _guard_context(
        self, progress: Progress, cancellation: RunCancellation
    ) -> TurnOutcome | None:
        """Заполненность перед запросом: сжатие, предохранители, блокирующий
        предел (план, раздел 5.16); None — запрос можно отправлять."""
        tracking = progress.tracking
        snapshot = self._snapshot_in_use()
        fill = self._gate.measure(self._session.conversation(), snapshot)
        decision = self._gate.decide(fill, tracking)
        if decision is GateDecision.RAPID_REFILL:
            await self._write_api_failure(
                self._templates.read(AgentTemplateFile.THRASHING)
            )
            return TurnOutcome.RAPID_REFILL_BREAKER

        if decision is GateDecision.COMPACT:
            compacted = await self._compact(fill, snapshot, cancellation)
            if isinstance(compacted, SummaryAborted):
                return self._abort_outcome(compacted.reason)

            if isinstance(compacted, Compacted):
                tracking.succeeded()
                return None

            tracking.failed(compacted.describe())

        return await self._blocked(fill, tracking)

    async def _blocked(
        self, fill: Fill, tracking: CompactTracking
    ) -> TurnOutcome | None:
        """Блокирующий предел: записи и исход, если сжатие не помогло."""
        if fill.level is not FillLevel.BLOCKED:
            return None

        if tracking.compacted:
            return None

        text = self._templates.read(AgentTemplateFile.PROMPT_TOO_LONG)
        if tracking.last_failure:
            text = self._templates.read(AgentTemplateFile.COMPACTION_FAILED).format(
                detail=tracking.last_failure
            )

        await self._write_api_error(text, 0, 0)
        await self._write_api_failure(text)
        if tracking.breaker_open:
            return TurnOutcome.COMPACT_FAILURE_BREAKER

        return TurnOutcome.BLOCKING_LIMIT

    async def _recover_overflow(
        self, asked: Overflowed, progress: Progress, cancellation: RunCancellation
    ) -> Settled | TurnOutcome:
        """Запрос не поместился в окно: сжатие и один повтор запроса; без
        сжатия исход решает обычный разбор переполнения."""
        snapshot = self._snapshot_in_use()
        fill = self._gate.measure(self._session.conversation(), snapshot)
        compacted = await self._compact(fill, snapshot, cancellation)
        if isinstance(compacted, SummaryAborted):
            return self._abort_outcome(compacted.reason)

        if isinstance(compacted, NotSummarized):
            progress.tracking.failed(compacted.describe())
            return asked

        progress.tracking.succeeded()
        snapshot = await self._announce_context()
        request = self._builder.build(self._session.conversation(), snapshot)

        return await self._ask(request, cancellation)

    async def _compact(
        self, fill: Fill, snapshot: PromptSnapshot, cancellation: RunCancellation
    ) -> CompactResult:
        """Пересказ и пачка записей; остановка во время пересказа записей не
        оставляет."""
        started = time.monotonic()
        conversation = self._session.conversation()
        self._session.enter(SessionState.COMPACTING)
        try:
            summarized = await self._summarizer.summarize(
                self._session, conversation, snapshot, cancellation
            )
        finally:
            self._session.enter(SessionState.TURN)

        if isinstance(summarized, NotSummarized):
            logger.warning("compaction failed: %s", summarized.describe())
            return summarized

        if isinstance(summarized, SummaryAborted):
            return summarized

        if cancellation.cancelled:
            return SummaryAborted(self._stop_reason_of(cancellation))

        duration = int((time.monotonic() - started) * self.MILLIS)
        records = self._compactor.records(
            self._session, summarized, fill.tokens, duration, self._attachments.today()
        )
        for record in records:
            await self._keeper.added(record)

        logger.info(
            "compaction: %d record(s) summarized, %d kept, %d tokens before",
            len(summarized.summarized),
            len(summarized.kept),
            fill.tokens,
        )

        return Compacted(records)

    def _snapshot_in_use(self) -> PromptSnapshot:
        """Снимок, по которому пойдёт запрос: из истории либо свежий."""
        snapshot = Announced(self._session.conversation()).snapshot()
        if snapshot is None:
            return self._fresh_snapshot()

        return snapshot

    @staticmethod
    def _stop_reason_of(cancellation: RunCancellation) -> StopReason:
        reason = cancellation.reason
        if reason is None:
            return StopReason.USER_STOP

        return reason

    @staticmethod
    def _abort_outcome(reason: StopReason) -> TurnOutcome:
        if reason is StopReason.SHUTDOWN:
            return TurnOutcome.SHUTDOWN

        return TurnOutcome.ABORTED_STREAMING

    async def _answered(
        self, asked: Answered, progress: Progress, cancellation: RunCancellation
    ) -> TurnOutcome | None:
        """Ответ записан; без вызовов ход завершён, с вызовами — исполнение."""
        progress.usage = self._summed(progress.usage, asked.reply.usage)
        planned = await self._write_reply(asked.reply, self._stop_reason(asked.reply))
        if not planned:
            return TurnOutcome.COMPLETED

        batch = await self._tools.run(planned, cancellation)
        await self._write_results(batch.results)
        if batch.aborted is not None:
            return await self._write_tool_abort(batch)

        await self._absorb()
        progress.tracking.tools_ran()

        return None

    async def _output_limited(
        self, asked: OutputLimited, progress: Progress
    ) -> TurnOutcome | None:
        progress.limit_hits += 1
        await self._write_partial(asked.partial, ReplyStopReason.MAX_TOKENS, False)
        if progress.limit_hits > self._limits.output_limit_retries:
            await self._write_api_failure(asked.error)
            return TurnOutcome.API_ERROR

        await self._write_meta(self._templates.read(AgentTemplateFile.OUTPUT_LIMIT_HIT))

        return None

    async def _ask(
        self, request: ChatRequest, cancellation: RunCancellation
    ) -> Settled:
        """Запрос с повторами: три попытки на ошибку порта, пауза между ними."""
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
        asked = await self._asking.attempt(request, cancellation, show=True)
        if isinstance(asked, Errored):
            self._last_failure = asked.error
            self._failures += 1
            await self._write_api_error(
                self._last_failure, self._failures, self._limits.api_retries
            )
            return None

        if isinstance(asked, Answered) and self._is_empty(asked.reply):
            self._last_failure = "the model returned an empty reply"
            logger.warning("turn: the model replied with nothing to record, retrying")
            return None

        return asked

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

    async def _announce_input(self) -> None:
        """Вложения точки «ввод» (раздел 5.17): окружение, модель, разница
        инструментов, список скиллов — каждое, только если есть что сообщить."""
        announced = Announced(self._session.conversation())
        if self._environment is not None:
            record = self._attachments.environment(
                self._session.last_chain_uuid(),
                self._environment,
                announced.environment(),
            )
            if record is not None:
                await self._keeper.add(record)

        if announced.model_id() != self._profile.model_id:
            record = self._attachments.model(
                self._session.last_chain_uuid(), self._profile
            )
            await self._keeper.add(record)

        await self._announce_tools(announced)
        await self._announce_skills(announced)

    async def _announce_tools(self, announced: Announced) -> None:
        """Список инструментов изменился относительно объявленного: разница
        вложением и новый снимок; без снимка в разговоре объявляет сам снимок."""
        known = announced.tool_names()
        if known is None:
            return

        current: dict[str, SnapshotTool] = {}
        for tool in self._snapshot_tools:
            current[tool.name] = tool

        added: list[SnapshotTool] = []
        for name, tool in current.items():
            if name not in known:
                added.append(tool)

        removed: list[str] = []
        for name in sorted(known):
            if name not in current:
                removed.append(name)

        if not added and not removed:
            return

        record = self._attachments.tools_delta(
            self._session.last_chain_uuid(), added, removed
        )
        await self._keeper.add(record)
        await self._keeper.add(
            self._attachments.snapshot(
                self._session.last_chain_uuid(), self._fresh_snapshot()
            )
        )

    async def _announce_skills(self, announced: Announced) -> None:
        known = announced.skill_names()
        fresh: list[SnapshotSkill] = []
        for skill in self._skills:
            if skill.name not in known:
                fresh.append(skill)

        if not fresh:
            return

        record = self._attachments.skill_listing(
            self._session.last_chain_uuid(), fresh, self._budget, not known
        )
        await self._keeper.add(record)

    def _fresh_snapshot(self) -> PromptSnapshot:
        return PromptSnapshot(
            system_prompt=self._system_prompt,
            tools=self._snapshot_tools,
            skills=self._skills,
            model=self._profile.model_id,
            system_turns=self._profile.system_turns,
        )

    async def _announce_context(self) -> PromptSnapshot:
        """Дата, если сменилась; снимок, если его нет в доступной истории."""
        announced = Announced(self._session.conversation())
        today = self._attachments.today()
        known = announced.date()
        if known != today:
            record = self._attachments.date(
                self._session.last_chain_uuid(), today, known is not None
            )
            await self._keeper.add(record)

        snapshot = announced.snapshot()
        if snapshot is None:
            snapshot = self._fresh_snapshot()
            record = self._attachments.snapshot(
                self._session.last_chain_uuid(), snapshot
            )
            await self._keeper.add(record)
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
        await self._keeper.add(record)

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
        await self._keeper.add(record)

    async def _write_reply(
        self, reply: ChatReply, stop_reason: ReplyStopReason, aborted: bool = False
    ) -> list[PlannedCall]:
        """Ответ модели — по записи на блок с общими `message.id`, `usage`;
        возвращает вызовы ответа с uuid их записей."""
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

        planned: list[PlannedCall] = []
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
            written = await self._keeper.add(record)
            if isinstance(block, ToolUseBlock):
                planned.append(PlannedCall(block=block, assistant_uuid=written.uuid))

        return planned

    async def _write_results(self, results: Sequence[CallResult]) -> None:
        """Результаты — по записи `user` на вызов, в порядке вызовов."""
        for result in results:
            is_error: bool | None = None
            if result.is_error:
                is_error = True

            block = ToolResultBlock(
                tool_use_id=result.call.block.id,
                content=result.text,
                is_error=is_error,
            )
            record = UserRecord(
                uuid=self._ids.uuid(),
                parent_uuid=self._session.last_chain_uuid(),
                timestamp=self._clock.now(),
                session_id=self._session.id.value,
                version=self._version,
                message=UserMessage(content=[block]),
                tool_use_result=result.artifact,
                source_tool_assistant_uuid=result.call.assistant_uuid,
                tool_denial_kind=result.denial,
            )
            await self._keeper.add(record)

    async def _absorb(self) -> None:
        """Элементы `now` и `next` из очереди — вложениями в разговор."""
        items = self._queue.absorb()
        if not items:
            return

        records = self._attachments.queued(
            self._session.last_chain_uuid(), items, self._agent
        )
        for record in records:
            await self._keeper.add(record)

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
        await self._keeper.add(record)

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
        await self._keeper.add(record)

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
        await self._keeper.add(record)

        return outcome

    async def _write_tool_abort(self, batch: BatchOutcome) -> TurnOutcome:
        """Записи отмены для стадии «инструменты работают» (раздел 5.14):
        результаты-отказы уже записаны, остаётся маркер."""
        if batch.aborted is StopReason.SUPERSEDED:
            return TurnOutcome.ABORTED_TOOLS

        by_shutdown: bool | None = None
        outcome = TurnOutcome.ABORTED_TOOLS
        if batch.aborted is StopReason.SHUTDOWN:
            by_shutdown = True
            outcome = TurnOutcome.SHUTDOWN

        text = self._templates.read(AgentTemplateFile.REQUEST_INTERRUPTED_TOOL_USE)
        record = UserRecord(
            uuid=self._ids.uuid(),
            parent_uuid=self._session.last_chain_uuid(),
            timestamp=self._clock.now(),
            session_id=self._session.id.value,
            version=self._version,
            message=UserMessage(content=text),
            interrupted_by_shutdown=by_shutdown,
        )
        await self._keeper.add(record)

        return outcome
