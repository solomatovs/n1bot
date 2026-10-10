"""Агент: связывает сессию, очередь, писатель, загрузчик, инструменты и цикл хода.

Один объект на процесс и на сессию. `open` захватывает сессию в хранилище,
при возобновлении восстанавливает её загрузчиком, сворачивает режим
разрешений под потолок параметров запуска, проверяет профиль и запускает
писатель; `serve` ждёт очередь и ведёт ходы один за другим, элемент `now`
посреди хода прерывает его; `answer` отдаёт ответ на вопрос наружу;
`set_permission_mode` меняет режим записью; `interrupt` останавливает
текущий ход; `close` гасит ход, сбрасывает журнал и отпускает сессию.
Переходы состояний — план, раздел 5.18.

Ошибки:
HistoryError — хранилище недоступно, сессия занята или журнал для
    возобновления пуст.
ProfileError — профиль модели не годится для запуска.
PermissionModeError — режим разрешений недоступен при параметрах запуска.
ToolsError — собственный инструмент одноимён с инструментом сервера, либо при
    возобновлении сервер не отдал ни одного инструмента из объявленных.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from typing import ClassVar

from boba.agent.attachments import (
    Announced,
    ContextAttachments,
    EnvironmentSnapshot,
    SnapshotSkill,
)
from boba.agent.builder import RequestBuilder
from boba.agent.control import ControlQuestions, PermissionAnswer
from boba.agent.events import EventSink, InitEvent, TurnOutcome
from boba.agent.history import HistoryError, HistoryStore, SessionId, SessionLease
from boba.agent.ids import Clock, IdMint, SystemClock
from boba.agent.loader import HistoryLoader, LoadReport
from boba.agent.permissions import AutoClassifier, ModeCeiling, PermissionState
from boba.agent.profile import (
    CompactionSettings,
    ModelProfile,
    ProfileCheck,
    TurnLimits,
)
from boba.agent.queue import InputQueue, QueueRemoveReason
from boba.agent.records import PermissionMode, PermissionModeRecord
from boba.agent.session import Session, SessionState
from boba.agent.skills import Skill, SkillTool
from boba.agent.template import AgentTemplate
from boba.agent.tools import (
    AgentTools,
    AskUserQuestionTool,
    OwnTool,
    ToolRunner,
    ToolsError,
    ToolTexts,
)
from boba.agent.turn import TurnLoop
from boba.agent.writer import HistoryWriter, RecordKeeper
from boba.cancellation import RunCancellation, StopReason
from boba.llm.chat import ChatModel
from boba.toolkit.dag import ToolServer

logger = logging.getLogger(__name__)

__all__ = ["Agent", "AgentParts"]


class AgentParts:
    """Зависимости агента, не привязанные к сессии."""

    DEFAULT_NAME: ClassVar[str] = "Boba"

    def __init__(  # noqa: PLR0913 — зависимости агента перечислены все разом
        self,
        profile: ModelProfile,
        limits: TurnLimits,
        compaction: CompactionSettings,
        system_prompt: Sequence[str],
        store: HistoryStore,
        model: ChatModel,
        sink: EventSink,
        version: str,
        tools: ToolServer | None = None,
        initial_mode: PermissionMode = PermissionMode.DEFAULT,
        allow_bypass: bool = False,
        classifier: ChatModel | None = None,
        classifier_instruction: str = "",
        agent_name: str = DEFAULT_NAME,
        skills: Sequence[Skill] = (),
        environment: EnvironmentSnapshot | None = None,
        templates: AgentTemplate | None = None,
        ids: IdMint | None = None,
        clock: Clock | None = None,
    ) -> None:
        self.profile = profile
        self.limits = limits
        self.compaction = compaction
        self.system_prompt = list(system_prompt)
        self.store = store
        self.model = model
        self.sink = sink
        self.version = version
        self.tools = tools
        self.initial_mode = initial_mode
        self.allow_bypass = allow_bypass
        self.classifier = classifier
        self.classifier_instruction = classifier_instruction
        self.agent_name = agent_name
        self.skills = list(skills)
        self.environment = environment
        self.templates = templates
        self.ids = ids
        self.clock = clock


class Wiring:
    """Части, которые агент собирает один раз на процесс: тексты, часы,
    чеканка, потолок режима, классификатор."""

    def __init__(self, parts: AgentParts) -> None:
        templates = parts.templates
        if templates is None:
            templates = AgentTemplate()

        ids = parts.ids
        if ids is None:
            ids = IdMint()

        clock = parts.clock
        if clock is None:
            clock = SystemClock()

        self.templates: AgentTemplate = templates
        self.ids: IdMint = ids
        self.clock: Clock = clock
        self.classifier: AutoClassifier | None = None
        if parts.classifier is not None:
            self.classifier = AutoClassifier(
                parts.classifier, parts.classifier_instruction, self.templates
            )

        self.ceiling = ModeCeiling(parts.allow_bypass, self.classifier is not None)


class Agent:
    """Процесс одной сессии: открыть, обслуживать очередь, остановить."""

    LOADER_LOG: ClassVar[str] = (
        "agent %s: resumed %d record(s), %d repaired, %d skipped"
    )

    def __init__(self, parts: AgentParts) -> None:
        self._parts = parts
        self._wiring = Wiring(parts)
        self._session: Session | None = None
        self._lease: SessionLease | None = None
        self._writer: HistoryWriter | None = None
        self._queue: InputQueue | None = None
        self._turns: TurnLoop | None = None
        self._questions: ControlQuestions | None = None
        self._current: RunCancellation | None = None
        self._stopping = False
        self._idle = asyncio.Event()
        self._idle.set()
        self._outcomes: list[TurnOutcome] = []

    @property
    def session(self) -> Session:
        if self._session is None:
            msg = "The agent session is not open yet. Call open() before using it."
            raise HistoryError(msg)

        return self._session

    @property
    def queue(self) -> InputQueue:
        if self._queue is None:
            msg = "The agent session is not open yet. Call open() before the queue."
            raise HistoryError(msg)

        return self._queue

    @property
    def writer(self) -> HistoryWriter:
        if self._writer is None:
            msg = "The agent session is not open yet. Call open() before the writer."
            raise HistoryError(msg)

        return self._writer

    @property
    def outcomes(self) -> Sequence[TurnOutcome]:
        return self._outcomes

    @property
    def permission_mode(self) -> PermissionMode:
        """Действующий режим — свёртка записей сессии."""
        return PermissionState(self.session.service()).mode

    async def open(self, session_id: SessionId, resume: bool) -> Session:
        parts = self._parts
        wiring = self._wiring
        lease = await parts.store.acquire(session_id)
        session = Session(session_id)
        try:
            report = await self._restored(session, resume)
            tools = AgentTools(parts.tools, self._own_tools())
            self._check_tools_survived(session, tools, resume)
            tool_texts: list[str] = []
            for tool in tools.snapshot():
                tool_texts.append(tool.text())

            ProfileCheck(parts.profile, parts.compaction).run(
                parts.system_prompt, tool_texts
            )
            mode = self._effective_mode(session, resume)
        except Exception:
            await parts.store.release(lease)
            raise

        self._session = session
        self._lease = lease
        writer = HistoryWriter(session, parts.store, lease, report.stored_seq)
        writer.start()
        self._writer = writer
        self._queue = InputQueue(session, wiring.clock, wiring.ids)
        self._questions = ControlQuestions(wiring.ids)
        if mode is not None:
            self._record_mode(mode)

        keeper = RecordKeeper(session, writer, parts.sink)
        texts = ToolTexts(
            wiring.templates, parts.limits.tool_result_max_chars, parts.agent_name
        )
        runner = ToolRunner(
            tools,
            texts,
            wiring.templates,
            self._questions,
            wiring.classifier,
            keeper,
            wiring.clock,
            parts.agent_name,
        )
        attachments = ContextAttachments(
            wiring.templates, wiring.ids, wiring.clock, parts.version
        )
        self._turns = TurnLoop(
            keeper,
            self._queue,
            parts.profile,
            parts.limits,
            parts.model,
            RequestBuilder(parts.profile, wiring.templates),
            attachments,
            runner,
            wiring.templates,
            wiring.ids,
            wiring.clock,
            parts.version,
            parts.sink,
            writer,
            parts.system_prompt,
            tools.snapshot(),
            self._skill_cards(),
            parts.environment,
            parts.agent_name,
        )

        tool_names: list[str] = []
        for card in tools.snapshot():
            tool_names.append(card.name)

        await parts.sink.emit(
            InitEvent(
                session_id=session_id.value,
                resumed=resume,
                model=parts.profile.model_id,
                tools=tool_names,
                permission_mode=self.permission_mode,
                version=parts.version,
            )
        )

        return session

    def _own_tools(self) -> Sequence[OwnTool]:
        """Собственные инструменты: вопрос пользователю всегда, Skill — при скиллах."""
        wiring = self._wiring
        texts = ToolTexts(
            wiring.templates,
            self._parts.limits.tool_result_max_chars,
            self._parts.agent_name,
        )
        own: list[OwnTool] = [AskUserQuestionTool(wiring.templates, texts)]
        if self._parts.skills:
            own.append(SkillTool(self._parts.skills, wiring.templates))

        return own

    def _skill_cards(self) -> Sequence[SnapshotSkill]:
        cards: list[SnapshotSkill] = []
        for skill in self._parts.skills:
            cards.append(skill.card())

        return cards

    def _check_tools_survived(
        self, session: Session, tools: AgentTools, resume: bool
    ) -> None:
        """Возобновление без единого инструмента сервера, когда история их
        объявляла, — сервер недоступен, а не список опустел: ошибка старта.

        Ошибки:
        ToolsError — сервер инструментов не отдал ничего при возобновлении.
        """
        if not resume:
            return

        known = Announced(session.conversation()).tool_names()
        if not known:
            return

        own: set[str] = set()
        for tool in self._own_tools():
            own.add(tool.name)

        offered = frozenset(card.name for card in tools.snapshot()) - own
        if offered:
            return

        if not (known - own):
            return

        msg = (
            f"Cannot resume session '{session.id.value}': the history announced "
            f"{len(known - own)} server tool(s), but the tool server offers none now. "
            "The server is probably unreachable; the agent will not record the tools "
            "as removed. Check the MCP server and start again."
        )
        raise ToolsError(msg)

    async def _restored(self, session: Session, resume: bool) -> LoadReport:
        """Ошибки:
        HistoryError — возобновляемая сессия без журнала.
        """
        if not resume:
            return LoadReport()

        parts = self._parts
        wiring = self._wiring
        loader = HistoryLoader(
            parts.store, wiring.templates, wiring.ids, wiring.clock, parts.version
        )
        report = await loader.load(session)
        if session.last_seq == 0:
            msg = (
                f"Cannot resume session '{session.id.value}': there is no journal to "
                "resume. Start a new session, or check that the history root points "
                "to the right place."
            )
            raise HistoryError(msg)

        logger.info(
            self.LOADER_LOG,
            session.id.value,
            report.stored_seq,
            len(report.repaired),
            report.skipped_lines + report.skipped_records,
        )

        return report

    def _effective_mode(self, session: Session, resume: bool) -> PermissionMode | None:
        """Режим, который надо записать при открытии; None — журнал уже несёт
        действующий режим и потолок его не трогает.

        Ошибки:
        PermissionModeError — режим `auto` без классификатора.
        """
        ceiling = self._wiring.ceiling
        if not resume:
            return ceiling.capped(self._parts.initial_mode)

        recorded = PermissionState(session.service()).mode
        capped = ceiling.capped(recorded)
        if capped is recorded:
            return None

        return capped

    def _record_mode(self, mode: PermissionMode) -> None:
        self.session.add(
            PermissionModeRecord(
                timestamp=self._wiring.clock.now(),
                session_id=self.session.id.value,
                permission_mode=mode,
            )
        )
        self.writer.notify()

    def set_permission_mode(self, mode: PermissionMode) -> PermissionMode:
        """Меняет режим записью; действует со следующей проверки разрешения.

        Ошибки:
        PermissionModeError — режим недоступен при параметрах запуска.
        """
        self._wiring.ceiling.check(mode)
        self._record_mode(mode)

        return mode

    def answer(self, request_id: str, answer: PermissionAnswer) -> bool:
        """Ответ на вопрос наружу; False — вопрос неизвестен или уже отвечен."""
        if self._questions is None:
            msg = "The agent session is not open yet. Call open() before answering."
            raise HistoryError(msg)

        return self._questions.answer(request_id, answer)

    async def serve(self) -> None:
        """Обслуживает очередь до `close()`: ход за ходом, между ними — ожидание."""
        queue = self.queue
        session = self.session
        turns = self._turns
        if turns is None:
            msg = "The agent session is not open yet. Call open() before serve()."
            raise HistoryError(msg)

        while not self._stopping:
            taken = queue.take()
            if taken is None:
                self._idle.set()
                await queue.wait_changed()
                continue

            self._idle.clear()
            cancellation = RunCancellation()
            self._current = cancellation
            session.enter(SessionState.TURN)
            watcher = asyncio.create_task(self._superseder(queue, cancellation))
            try:
                outcome = await turns.run(taken, cancellation)
            finally:
                watcher.cancel()
                self._current = None
                session.enter(SessionState.IDLE)

            self._outcomes.append(outcome)
            failure = self.writer.failed()
            if failure is not None:
                msg = (
                    f"The history writer stopped unexpectedly: {failure}. The journal "
                    "is no longer written, so the agent stops instead of running blind."
                )
                raise HistoryError(msg) from failure

        self._idle.set()

    async def _superseder(
        self, queue: InputQueue, cancellation: RunCancellation
    ) -> None:
        """Элемент `now` посреди хода прерывает ход причиной SUPERSEDED."""
        await queue.now_arrived.wait()
        cancellation.cancel(StopReason.SUPERSEDED)

    async def settled(self) -> None:
        """Ждёт, пока очередь опустеет и ход закончится."""
        while True:
            await self._idle.wait()
            if len(self.queue) == 0:
                return

            await asyncio.sleep(0)

    def interrupt(
        self, reason: StopReason = StopReason.USER_STOP, cancel_queued: bool = False
    ) -> bool:
        """Останавливает текущий ход; True — ход был и остановлен."""
        if cancel_queued:
            self.queue.clear(QueueRemoveReason.CLEARED_ON_CANCEL)

        current = self._current
        if current is None:
            return False

        current.cancel(reason)

        return True

    async def close(self) -> None:
        """Гасит ход причиной SHUTDOWN, сбрасывает журнал, отпускает сессию."""
        self._stopping = True
        self.interrupt(StopReason.SHUTDOWN)
        if self._session is not None:
            self._session.enter(SessionState.STOPPING)

        if self._queue is not None:
            self._queue.wake()

        await self._idle.wait()
        if self._writer is not None:
            await self._writer.stop()

        if self._lease is not None:
            await self._parts.store.release(self._lease)
            self._lease = None
