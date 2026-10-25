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
from typing import Any, ClassVar

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
    ToolPoolChange,
)
from boba.agent.builder import RequestBuilder
from boba.agent.compaction import (
    CompactGate,
    CompactJob,
    Compactor,
    CompactTracking,
    ContextMeter,
    Fill,
    FillLevel,
    GateDecision,
    NotSummarized,
    Summarizer,
    SummaryAborted,
    SummaryFailure,
    SummaryInstruction,
    SummaryJob,
    SummaryPressure,
    TokenEstimator,
)
from boba.agent.events import EventSink, ResultEvent
from boba.agent.ids import Clock, IdMint
from boba.agent.profile import CompactionSettings, ModelProfile, TurnLimits
from boba.agent.queue import InputQueue, QueueItem, QueueKind, Taken
from boba.agent.records import (
    ApiErrorRecord,
    AssistantBlock,
    AssistantMessage,
    CommandOutputRecord,
    CommandRecord,
    CompactionPhase,
    CompactionRecord,
    CompactTrigger,
    InformationalRecord,
    InterruptRecord,
    MetaRecord,
    NoticeLevel,
    PromptRecord,
    Record,
    ReplyRecord,
    ReplyStopReason,
    TaskNotificationRecord,
    TextBlock,
    ThinkingBlock,
    ToolReferenceBlock,
    ToolResultBlock,
    ToolResultRecord,
    ToolUseBlock,
    TurnEndRecord,
    TurnOutcome,
    Usage,
    UserMessage,
)
from boba.agent.session import Session, SessionState
from boba.agent.tags import AgentTag, LocalCommandText
from boba.agent.template import AgentTemplate, AgentTemplateFile, ModelTroubleTexts
from boba.agent.tools import (
    AgentTools,
    BatchOutcome,
    CallResult,
    PlannedCall,
    ToolRunner,
)
from boba.agent.writer import (
    ClientCommands,
    HistoryWriter,
    RecordKeeper,
    RequestRecorder,
)
from boba.cancellation import RunCancellation, StopReason
from boba.core.commands import CommandCall
from boba.llm.chat import (
    ChatModel,
    ChatReply,
    ChatRequest,
    ChatUsage,
    ContextOverflowSigns,
    LlmTrouble,
)

logger = logging.getLogger(__name__)

__all__ = ["ContextUsage", "TurnLoop"]


@dataclass(frozen=True)
class Failed:
    """Повторы запроса исчерпаны: последняя неудачная попытка."""

    attempt: Errored


Settled = Asked | Failed


@dataclass(frozen=True)
class Compacted:
    """Сжатие состоялось: записи пачки уже в сессии."""

    records: Sequence[Record]


CompactResult = Compacted | NotSummarized | SummaryAborted


@dataclass(frozen=True)
class ContextUsage:
    """Заполненность контекста: оценка разговора в токенах и действующее
    окно модели без запаса под ответ."""

    used: int
    size: int


@dataclass
class Progress:
    """Счётчики хода: шаги, обрывы по длине, суммарный учёт, предохранители сжатия."""

    steps: int = 0
    limit_hits: int = 0
    usage: Usage = field(default_factory=Usage)
    tracking: CompactTracking = field(default_factory=CompactTracking)
    notice: TurnNotice = field(default_factory=lambda: TurnNotice(text="", detail=""))
    """Слова человеку о причине конца хода; пусто — ход кончился штатно."""


@dataclass(frozen=True)
class TurnNotice:
    """Пометка конца хода: слова человеку и короткая техническая причина."""

    text: str
    detail: str


@dataclass(frozen=True)
class CompactionStep:
    """Идущее сжатие для его записей compaction: id, триггер и запись вызова
    `/compact` (None — сжатие по порогу)."""

    compaction_id: str
    trigger: CompactTrigger
    command_uuid: str | None


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
        catalog: AgentTools,
        skills: Sequence[SnapshotSkill],
        environment: EnvironmentSnapshot | None,
        agent_name: str,
        recorder: RequestRecorder,
        client_commands: ClientCommands,
    ) -> None:
        self._recorder = recorder
        self._client_commands = client_commands
        self._request_id = ""
        self._message_id = ""
        self._keeper = keeper
        self._session: Session = keeper.session
        self._queue = queue
        self._limits = limits
        self._compaction = compaction
        self._attachments = attachments
        self._tools = tools
        self._templates = templates
        self._troubles = ModelTroubleTexts(templates)
        self._ids = ids
        self._clock = clock
        self._version = version
        self._sink = sink
        self._writer = writer
        self._system_prompt = list(system_prompt)
        self._catalog = catalog
        self._skills = list(skills)
        self._environment = environment
        self._agent = agent_name
        self._commands = LocalCommandText()
        self._last_failure = ""
        self._overflow_signs = ContextOverflowSigns()
        self._bind_model(profile, model, builder)

    def _bind_model(
        self, profile: ModelProfile, model: ChatModel, builder: RequestBuilder
    ) -> None:
        """Части, которые зависят от профиля модели: сборщик запроса, бюджет
        скиллов, обращение к модели, оценка заполненности, пересказ и пачка
        сжатия. Собираются на старте и заново при смене модели."""
        self._profile = profile
        self._builder = builder
        self._budget = SkillBudget(profile.context_window, profile.chars_per_token)
        self._asking = ModelAsk(model, self._sink)
        estimator = TokenEstimator(profile, self._templates)
        self._estimator = estimator
        self._gate = CompactGate(
            ContextMeter(estimator),
            self._compaction,
            profile,
            SummaryInstruction(self._templates).text(""),
        )
        self._summarizer = Summarizer(
            self._asking,
            builder,
            self._templates,
            estimator,
            self._ids,
            self._clock,
            self._version,
            self._recorder,
        )
        self._compactor = Compactor(
            self._attachments,
            estimator,
            self._templates,
            self._ids,
            self._clock,
            self._version,
            profile,
            self._environment,
            self._skills,
            self._budget,
            self._catalog,
        )

    def set_model(
        self, profile: ModelProfile, model: ChatModel, builder: RequestBuilder
    ) -> None:
        """Смена профиля модели между ходами: следующий запрос идёт новой
        модели с её окном и сэмплингом; вложение `model` объявит смену в
        начале следующего хода. Вызывающий проверяет, что ход не идёт."""
        self._bind_model(profile, model, builder)

    @property
    def profile(self) -> ModelProfile:
        return self._profile

    def set_system_prompt(self, sections: Sequence[str]) -> None:
        """Системный промпт запуска; действует с первого снимка, который ещё
        не записан (новая сессия, следующее сжатие)."""
        self._system_prompt = list(sections)

    @property
    def system_prompt(self) -> Sequence[str]:
        return self._system_prompt

    def context_usage(self) -> ContextUsage:
        """Текущая оценка заполненности: как её видит проверка перед запросом."""
        fill = self._gate.measure(self._session.conversation(), self._snapshot_in_use())

        return ContextUsage(used=fill.tokens, size=self._profile.effective_window())

    async def run(self, taken: Taken, cancellation: RunCancellation) -> ResultEvent:
        """Ход по взятому из очереди; последняя запись хода — turn_end с
        исходом. Итог отдаётся, а не объявляется: конец хода объявляет агент,
        когда записи сброшены и сессия уже свободна."""
        started = time.monotonic()
        self._last_failure = ""
        progress = Progress()
        self._client_commands.opened()
        if taken.head.kind is QueueKind.COMPACT:
            outcome = await self._manual_compact(taken.head, progress, cancellation)
        else:
            outcome = await self._turn(taken, progress, cancellation)

        await self._client_commands.closed()
        duration = int((time.monotonic() - started) * self.MILLIS)
        error = ""
        if outcome.refused:
            error = self._last_failure

        turn_end = self._keeper.add_unannounced(
            TurnEndRecord(
                uuid=self._ids.uuid(),
                parent_uuid=self._session.last_chain_uuid(),
                timestamp=self._clock.now(),
                session_id=self._session.id.value,
                version=self._version,
                outcome=outcome,
                notice=progress.notice.text,
                level=NoticeLevel.ERROR,
                detail=progress.notice.detail,
                error=error,
                usage=progress.usage,
                num_turns=progress.steps,
                duration_ms=duration,
            )
        )
        await self._writer.flush()

        return ResultEvent(turn_end=turn_end)

    async def _turn(
        self, taken: Taken, progress: Progress, cancellation: RunCancellation
    ) -> TurnOutcome:
        """Ход по вопросу: запись вопроса, вложения ввода, шаги до исхода."""
        await self._write_prompt(taken)
        for item in taken.items:
            for text in item.context:
                await self._write_meta(text)

        await self._absorb()
        await self._announce_input()

        while True:
            progress.steps += 1
            outcome = await self._step(progress, cancellation)
            if outcome is not None:
                return outcome

    async def _manual_compact(
        self, command: QueueItem, progress: Progress, cancellation: RunCancellation
    ) -> TurnOutcome:
        """Сжатие по команде пользователя `/compact <указания>`: вызов
        пишется в разговор записями локальной команды — пометка о ней и сам
        вызов с uuid элемента очереди, — затем пересказ с указаниями из
        текста команды и запись её вывода. Вызов остаётся хвостом после
        пересказа, модель видит его в следующих запросах. Неудача — пометка
        конца хода с причиной, как у отказа модели."""
        await self._write_command(command)
        snapshot = self._snapshot_in_use()
        fill = self._gate.measure(self._session.conversation(), snapshot)
        pressure = SummaryPressure(
            trigger=CompactTrigger.MANUAL,
            last_resort=True,
            excess=None,
            instructions=command.text,
        )
        compacted = await self._compact(
            fill, snapshot, pressure, cancellation, command.id
        )
        if isinstance(compacted, SummaryAborted):
            return self._abort_outcome(compacted.reason)

        if isinstance(compacted, NotSummarized):
            text = self._templates.read(AgentTemplateFile.MANUAL_COMPACTION_FAILED)
            progress.notice = TurnNotice(
                text=text.format(detail=compacted.brief()), detail=compacted.describe()
            )
            return TurnOutcome.API_ERROR

        output = self._templates.read(AgentTemplateFile.COMPACTED)
        await self._keeper.add(
            CommandOutputRecord(
                uuid=self._ids.uuid(),
                parent_uuid=self._session.last_chain_uuid(),
                timestamp=self._clock.now(),
                session_id=self._session.id.value,
                version=self._version,
                message=UserMessage(content=AgentTag.LOCAL_COMMAND_STDOUT.wrap(output)),
            )
        )

        return TurnOutcome.COMPLETED

    async def _write_command(self, command: QueueItem) -> None:
        """Записи вызова локальной команды: пометка для модели (служебная) и
        вызов тегами с uuid элемента очереди — его вставку видит клиент."""
        caveat = self._templates.read(AgentTemplateFile.LOCAL_COMMAND_CAVEAT)
        await self._write_meta(
            AgentTag.LOCAL_COMMAND_CAVEAT.wrap(caveat.format(agent=self._agent))
        )

        call = CommandCall(name=command.kind.value, rest=command.text)
        record = CommandRecord(
            uuid=command.id,
            parent_uuid=self._session.last_chain_uuid(),
            timestamp=self._clock.now(),
            session_id=self._session.id.value,
            version=self._version,
            message=UserMessage(content=self._commands.call(call)),
        )
        await self._keeper.add(record)

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
            self._last_failure = asked.error

            return self._too_long_outcome(progress)

        if isinstance(asked, Failed):
            failed = asked.attempt
            progress.notice = TurnNotice(
                text=self._troubles.failed(failed.trouble), detail=failed.detail
            )
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
            progress.notice = TurnNotice(
                text=self._templates.read(AgentTemplateFile.WINDOW_THRASHING_NOTICE),
                detail="",
            )
            return TurnOutcome.RAPID_REFILL_BREAKER

        if decision is GateDecision.COMPACT:
            pressure = SummaryPressure(
                trigger=CompactTrigger.AUTO,
                last_resort=fill.level is FillLevel.BLOCKED,
                excess=None,
                instructions="",
            )
            compacted = await self._compact(
                fill, snapshot, pressure, cancellation, None
            )
            if isinstance(compacted, SummaryAborted):
                return self._abort_outcome(compacted.reason)

            if isinstance(compacted, Compacted):
                tracking.succeeded()
                return None

            tracking.failed(compacted)

        return self._blocked(fill, progress)

    def _blocked(self, fill: Fill, progress: Progress) -> TurnOutcome | None:
        """Блокирующий предел: исход, если сжатие не помогло."""
        if fill.level is not FillLevel.BLOCKED:
            return None

        if progress.tracking.compacted:
            return None

        self._last_failure = self._too_long(progress.tracking)

        return self._too_long_outcome(progress)

    def _too_long_outcome(self, progress: Progress) -> TurnOutcome:
        """Конец хода запросом больше окна: пометка человеку — окно полно, а
        если сжатие в этом ходе не удалось — что оно не помогло, с короткой
        причиной."""
        tracking = progress.tracking
        notice = self._templates.read(AgentTemplateFile.WINDOW_FULL_NOTICE)
        detail = ""
        if tracking.last_failure:
            notice = self._templates.read(AgentTemplateFile.WINDOW_UNSHORTENED_NOTICE)
            detail = tracking.last_reason

        progress.notice = TurnNotice(text=notice, detail=detail)
        if tracking.breaker_open:
            return TurnOutcome.COMPACT_FAILURE_BREAKER

        return TurnOutcome.BLOCKING_LIMIT

    def _too_long(self, tracking: CompactTracking) -> str:
        """Текст записи о запросе больше окна: разговор не помещается и
        после сжатия — просьба начать новый чат; иначе с причиной последней
        неудачи сжатия, если оно в этом ходе не удалось."""
        if tracking.exhausted:
            return self._templates.read(AgentTemplateFile.THREAD_EXHAUSTED).format(
                detail=tracking.last_detail
            )

        if tracking.last_failure:
            return self._templates.read(AgentTemplateFile.COMPACTION_FAILED).format(
                detail=tracking.last_failure
            )

        return self._templates.read(AgentTemplateFile.PROMPT_TOO_LONG)

    async def _recover_overflow(
        self, asked: Overflowed, progress: Progress, cancellation: RunCancellation
    ) -> Settled | TurnOutcome:
        """Запрос не поместился в окно: сжатие и один повтор запроса; без
        сжатия исход решает обычный разбор переполнения. После трёх неудач
        хода сжатие больше не пробуется."""
        if progress.tracking.breaker_open:
            return asked

        snapshot = self._snapshot_in_use()
        fill = self._gate.measure(self._session.conversation(), snapshot)
        pressure = SummaryPressure(
            trigger=CompactTrigger.AUTO,
            last_resort=True,
            excess=self._overflow_signs.excess(asked.error),
            instructions="",
        )
        compacted = await self._compact(fill, snapshot, pressure, cancellation, None)
        if isinstance(compacted, SummaryAborted):
            return self._abort_outcome(compacted.reason)

        if isinstance(compacted, NotSummarized):
            progress.tracking.failed(compacted)
            return asked

        progress.tracking.succeeded()
        snapshot = await self._announce_context()
        request = self._builder.build(self._session.conversation(), snapshot)

        return await self._ask(request, cancellation)

    async def _compact(
        self,
        fill: Fill,
        snapshot: PromptSnapshot,
        pressure: SummaryPressure,
        cancellation: RunCancellation,
        command_uuid: str | None,
    ) -> CompactResult:
        """Пересказ и пачка записей. Ход сжатия пишется в историю записями
        compaction: начато, не удалось с причиной, прервано; состоявшееся —
        граница и пересказ с uuid, равным id сжатия. command_uuid — запись
        вызова `/compact` у сжатия по команде."""
        started = time.monotonic()
        job = SummaryJob(
            session=self._session,
            conversation=self._session.conversation(),
            snapshot=snapshot,
            cancellation=cancellation,
            pressure=pressure,
        )
        unready = self._summarizer.unready(job)
        if unready is not None:
            logger.info("compaction skipped: %s", unready.describe())
            return unready

        phase = CompactionStep(
            compaction_id=self._ids.uuid(),
            trigger=pressure.trigger,
            command_uuid=command_uuid,
        )
        await self._write_compaction(phase, CompactionPhase.STARTED, "")
        self._session.enter(SessionState.COMPACTING)
        try:
            summarized = await self._summarizer.summarize(job)
        finally:
            self._session.enter(SessionState.TURN)

        if isinstance(summarized, SummaryAborted):
            return await self._compaction_aborted(phase, summarized)

        if cancellation.cancelled:
            aborted = SummaryAborted(self._stop_reason_of(cancellation))
            return await self._compaction_aborted(phase, aborted)

        if isinstance(summarized, NotSummarized):
            return await self._compaction_failed(phase, summarized)

        duration = int((time.monotonic() - started) * self.MILLIS)
        compact_job = CompactJob(
            compaction_id=phase.compaction_id,
            trigger=pressure.trigger,
            pre_tokens=fill.tokens,
            duration_ms=duration,
            today=self._attachments.today(),
        )
        pack = self._compactor.pack(self._session, summarized, compact_job)
        fit = self._estimator.prefix(self._fresh_snapshot()) + pack.post_tokens
        window = self._profile.effective_window()
        if fit > window:
            detail = (
                f"after compaction the request needs about {fit} tokens, the window "
                f"holds {window}"
            )
            failure = NotSummarized(SummaryFailure.DOES_NOT_FIT, detail)
            return await self._compaction_failed(phase, failure)

        records = self._compactor.add(self._session, pack)
        for record in records:
            await self._keeper.added(record)

        logger.info(
            "compaction: %d record(s) summarized, %d kept, %d tokens before",
            len(summarized.summarized),
            len(summarized.kept),
            fill.tokens,
        )

        return Compacted(records)

    async def _compaction_failed(
        self, phase: CompactionStep, failure: NotSummarized
    ) -> NotSummarized:
        logger.warning("compaction failed: %s", failure.describe())
        await self._write_compaction(phase, CompactionPhase.FAILED, failure.describe())

        return failure

    async def _compaction_aborted(
        self, phase: CompactionStep, aborted: SummaryAborted
    ) -> SummaryAborted:
        await self._write_compaction(phase, CompactionPhase.CANCELLED, "")

        return aborted

    async def _write_compaction(
        self, step: CompactionStep, phase: CompactionPhase, error: str
    ) -> None:
        record = CompactionRecord(
            uuid=self._ids.uuid(),
            parent_uuid=self._session.last_chain_uuid(),
            timestamp=self._clock.now(),
            session_id=self._session.id.value,
            version=self._version,
            compaction_id=step.compaction_id,
            phase=phase,
            trigger=step.trigger,
            command_uuid=step.command_uuid,
            error=error,
        )
        await self._keeper.add(record)

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
            self._last_failure = asked.error
            progress.notice = TurnNotice(
                text=self._templates.read(AgentTemplateFile.OUTPUT_LIMIT_NOTICE),
                detail="",
            )
            return TurnOutcome.OUTPUT_LIMIT

        await self._write_meta(self._templates.read(AgentTemplateFile.OUTPUT_LIMIT_HIT))

        return None

    async def _ask(
        self, request: ChatRequest, cancellation: RunCancellation
    ) -> Settled:
        """Запрос с повторами: три попытки на ошибку порта, пауза между ними."""
        self._request_id = await self._recorder.record(request)
        attempt = 0
        while True:
            asked = await self._attempt(request, cancellation)
            if not isinstance(asked, Errored):
                return asked

            self._last_failure = asked.error
            attempt += 1
            if attempt > self._limits.api_retries:
                return Failed(asked)

            delay = self._limits.delay_before(attempt)
            await self._write_api_error(asked, int(delay * self.MILLIS), attempt)
            await asyncio.sleep(delay)

    async def _attempt(
        self, request: ChatRequest, cancellation: RunCancellation
    ) -> Asked:
        """Одна попытка; Errored — ошибка, которую стоит повторить, пустой
        ответ — тоже. Идентификатор сообщения ответа выдаётся здесь, до
        первого куска наружу."""
        self._message_id = self._ids.message_id()
        asked = await self._asking.attempt(
            request, cancellation, show=True, message_id=self._message_id
        )
        if not isinstance(asked, Answered):
            return asked

        if not self._is_empty(asked.reply):
            return asked

        logger.warning("turn: the model replied with nothing to record, retrying")

        return Errored("the model returned an empty reply", LlmTrouble.EMPTY, "")

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
        инструментов, инструкции серверов MCP, список скиллов — каждое,
        только если есть что сообщить. Перед этим устаревшее у серверов MCP
        перечитывается: свежесть проверяется там, где списки нужны."""
        for notice in await self._catalog.refreshed():
            await self._write_notice(notice.level, notice.text, notice.detail)

        announced = Announced(self._session.conversation())
        if self._environment is not None:
            record = self._attachments.environment(
                self._session.last_chain_uuid(),
                self._environment,
                announced.environment_snapshot(),
            )
            if record is not None:
                await self._keeper.add(record)

        if announced.model_id() != self._profile.model_id:
            record = self._attachments.model(
                self._session.last_chain_uuid(), self._profile
            )
            await self._keeper.add(record)

        await self._announce_tools(announced)
        await self._announce_instructions(announced)
        await self._announce_skills(announced)

    async def _announce_tools(self, announced: Announced) -> None:
        """Набор инструментов изменился относительно объявленного: разница
        именами вложением deferred_tools_delta; набор с полным определением
        сменился — новый снимок. Без снимка в разговоре его объявит
        `_announce_context`, а вложение называет только отложенные.
        Отложенный, которого разговор уже объявлял и снимал, объявляется
        вернувшимся."""
        inline: list[str] = []
        for tool in self._catalog.inline():
            inline.append(tool.name)

        deferred: list[str] = []
        for tool in self._catalog.deferred():
            deferred.append(tool.name)

        inline_known = announced.inline_names()
        pool = announced.deferred_pool()
        withdrawn = announced.withdrawn_tools()
        added = sorted(set(deferred) - pool)
        readded = withdrawn.intersection(added)
        surfaced: list[str] = []
        known = set(pool)
        if inline_known is not None:
            surfaced = sorted(set(inline) - inline_known)
            known.update(inline_known)

        removed = sorted(known - set(inline) - set(deferred))
        change = ToolPoolChange(
            added=added,
            removed=removed,
            readded=readded,
            surfaced=surfaced,
            search=bool(deferred),
        )
        if not change.empty():
            record = self._attachments.deferred_delta(
                self._session.last_chain_uuid(), change
            )
            await self._keeper.add(record)

        if inline_known is None:
            return

        if inline_known == frozenset(inline):
            return

        await self._keeper.add(
            self._attachments.snapshot(
                self._session.last_chain_uuid(), self._fresh_snapshot()
            )
        )

    async def _announce_instructions(self, announced: Announced) -> None:
        """Инструкции подключённых серверов MCP относительно объявленных в
        разговоре: новые и сменившиеся — блоками, отключившиеся — именами."""
        record = self._attachments.instructions(
            self._session.last_chain_uuid(),
            self._catalog.connected(),
            announced.announced_instructions(),
        )
        if record is None:
            return

        await self._keeper.add(record)

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
            tools=self._catalog.inline(),
            skills=self._skills,
            model=self._profile.model_id,
            system_turns=self._profile.system_turns,
        )

    async def _announce_context(self) -> PromptSnapshot:
        """Дата, если сменилась; снимок, если его нет в доступной истории."""
        announced = Announced(self._session.conversation())
        today = self._attachments.today()
        known = announced.date_text()
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
        elif snapshot.system_prompt != self._system_prompt:
            logger.warning(
                "turn: the system prompt of the launch differs from the snapshot in "
                "history; the request follows the snapshot"
            )

        await self._record_deferred()

        return snapshot

    async def _record_deferred(self) -> None:
        """Определения загруженных отложенных инструментов, которых история
        ещё не записала, — записью deferred_tools_record из живого набора:
        запрос собирается из истории, и возобновление шлёт те же байты.
        Инструмент, которого в живом наборе уже нет, не записывается и в
        запрос не уходит."""
        missing = Announced(self._session.conversation()).deferred_unrecorded()
        if not missing:
            return

        live: dict[str, SnapshotTool] = {}
        for tool in self._catalog.deferred():
            live[tool.name] = tool

        entries: list[SnapshotTool] = []
        for name in missing:
            tool = live.get(name)
            if tool is None:
                continue

            entries.append(tool)

        if not entries:
            return

        record = self._attachments.deferred_record(
            self._session.last_chain_uuid(), entries
        )
        await self._keeper.add(record)

    async def _write_prompt(self, taken: Taken) -> None:
        # uuid записи — идентификатор головного элемента очереди: его либо дал
        # отправитель (и потом ссылается на запись по нему), либо отчеканила очередь
        message = UserMessage(content=taken.content)
        if taken.head.kind is QueueKind.TASK_NOTIFICATION:
            await self._keeper.add(
                TaskNotificationRecord(
                    uuid=taken.head.id,
                    parent_uuid=self._session.last_chain_uuid(),
                    timestamp=self._clock.now(),
                    session_id=self._session.id.value,
                    version=self._version,
                    message=message,
                )
            )
            return

        await self._keeper.add(
            PromptRecord(
                uuid=taken.head.id,
                parent_uuid=self._session.last_chain_uuid(),
                timestamp=self._clock.now(),
                session_id=self._session.id.value,
                version=self._version,
                message=message,
            )
        )

    async def _write_meta(self, text: str) -> None:
        record = MetaRecord(
            uuid=self._ids.uuid(),
            parent_uuid=self._session.last_chain_uuid(),
            timestamp=self._clock.now(),
            session_id=self._session.id.value,
            version=self._version,
            message=UserMessage(content=text),
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

        message_id = self._message_id
        if not message_id:
            message_id = self._ids.message_id()

        request_id = self._request_id
        if not request_id:
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
            source = await self._source(block, aborted)
            record = ReplyRecord(
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
                tool_source=source,
            )
            written = await self._keeper.add(record)
            if isinstance(block, ToolUseBlock):
                planned.append(PlannedCall(block=block, assistant_uuid=written.uuid))

        return planned

    async def _source(
        self, block: AssistantBlock, aborted: bool
    ) -> dict[str, Any] | None:
        """Источник показа вызова у его сервера; у оборванного ответа вызовы
        не исполняются, и источник не спрашивается."""
        if not isinstance(block, ToolUseBlock):
            return None

        if aborted:
            return None

        source = await self._catalog.source(block)
        if source is None:
            return None

        return dict(source)

    async def _write_results(self, results: Sequence[CallResult]) -> None:
        """Результаты — по записи `user` на вызов, в порядке вызовов."""
        for result in results:
            is_error: bool | None = None
            if result.is_error:
                is_error = True

            block = ToolResultBlock(
                tool_use_id=result.call.block.id,
                content=self._result_content(result),
                is_error=is_error,
            )
            record = ToolResultRecord(
                uuid=self._ids.uuid(),
                parent_uuid=self._session.last_chain_uuid(),
                timestamp=self._clock.now(),
                session_id=self._session.id.value,
                version=self._version,
                message=UserMessage(content=[block]),
                tool_use_result=result.stored,
                source_tool_assistant_uuid=result.call.assistant_uuid,
                tool_denial_kind=result.denial,
                status=result.status,
            )
            await self._keeper.add(record)

    @staticmethod
    def _result_content(
        result: CallResult,
    ) -> str | Sequence[TextBlock | ToolReferenceBlock]:
        """Текст результата; у результата со ссылками — блоки tool_reference,
        затем текст, если он есть."""
        if not result.references:
            return result.text

        blocks: list[TextBlock | ToolReferenceBlock] = []
        for name in result.references:
            blocks.append(ToolReferenceBlock(tool_name=name))

        if result.text:
            blocks.append(TextBlock(text=result.text))

        return blocks

    async def _absorb(self) -> None:
        """Граница шага: команды клиента, ждавшие её, затем элементы `now` и
        `next` из очереди — вложениями в разговор, за ними служебный
        контекст элементов, как у вопроса хода."""
        await self._client_commands.boundary()
        items = self._queue.absorb()
        if not items:
            return

        records = self._attachments.queued(
            self._session.last_chain_uuid(), items, self._agent
        )
        for record in records:
            await self._keeper.add(record)

        for item in items:
            for text in item.context:
                await self._write_meta(text)

    async def _write_partial(
        self, partial: Partial, stop_reason: ReplyStopReason, aborted: bool
    ) -> None:
        if partial.is_empty():
            return

        await self._write_reply(partial.as_reply(), stop_reason, aborted)

    async def _write_api_error(
        self, failed: Errored, retry_in_ms: int, attempt: int
    ) -> None:
        """Запись system api_error: попытка attempt (с 1) не удалась, повтор
        через retry_in_ms; отказ без повтора пишет пометка конца хода."""
        record = ApiErrorRecord(
            uuid=self._ids.uuid(),
            parent_uuid=self._session.last_chain_uuid(),
            timestamp=self._clock.now(),
            session_id=self._session.id.value,
            version=self._version,
            error=failed.error,
            retry_in_ms=retry_in_ms,
            retry_attempt=attempt,
            max_retries=self._limits.api_retries,
            trouble=failed.trouble,
            detail=failed.detail,
        )
        await self._keeper.add(record)

    async def _write_notice(
        self, level: NoticeLevel, content: str, detail: str
    ) -> None:
        """Пометка для человека: записью informational в ход."""
        record = InformationalRecord(
            uuid=self._ids.uuid(),
            parent_uuid=self._session.last_chain_uuid(),
            timestamp=self._clock.now(),
            session_id=self._session.id.value,
            version=self._version,
            content=content,
            level=level,
            detail=detail,
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

        record = InterruptRecord(
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
        record = InterruptRecord(
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
