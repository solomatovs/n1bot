"""Агент: связывает сессию, очередь, писатель, загрузчик, инструменты и цикл хода.

Один объект на процесс и на сессию. `open` захватывает сессию в хранилище,
при возобновлении восстанавливает её загрузчиком, сворачивает режим
разрешений под потолок параметров запуска, проверяет профиль и запускает
писатель; `serve` ждёт очередь и ведёт ходы один за другим, элемент `now`
посреди хода прерывает его; `record_command` пишет в историю команду,
которую клиент исполнил сам, на ближайшей границе хода; `answer` отдаёт
ответ на вопрос наружу; `set_permission_mode` меняет режим записью,
`available_modes` перечисляет режимы под потолком запуска; `set_model`
переключает профиль модели между ходами; `interrupt` останавливает
текущий ход; `close` гасит ход, сбрасывает журнал и отпускает сессию;
`tool_hints` и `context_usage` отдают приёмнику событий пометки
инструмента и заполненность контекста.
Переходы состояний — план, раздел 5.18.

Ошибки:
HistoryError — хранилище недоступно или отказало.
SessionHeldError — сессию держит другой процесс (подвид HistoryError).
SessionNotFoundError — журнал для возобновления пуст (подвид HistoryError).
ProfileError — профиль модели не годится для запуска или для переключения.
PermissionModeError — режим разрешений недоступен при параметрах запуска.
ToolsError — собственный инструмент одноимён с инструментом сервера, либо при
    возобновлении сервер не отдал ни одного инструмента из объявленных.
SessionError — системный промпт или модель меняют посреди хода.
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
from boba.agent.compaction import SummaryInstruction
from boba.agent.control import ControlQuestions, PermissionAnswer
from boba.agent.events import EventSink, InitEvent
from boba.agent.history import (
    HistoryError,
    HistoryStore,
    SessionId,
    SessionLease,
    SessionNotFoundError,
)
from boba.agent.ids import Clock, IdMint, SystemClock
from boba.agent.loader import HistoryLoader, LoadReport
from boba.agent.mcp import ToolHints
from boba.agent.permissions import (
    AutoClassifier,
    ModeCeiling,
    PermissionState,
    StaticRules,
)
from boba.agent.profile import (
    CompactionSettings,
    ModelProfile,
    ProfileCheck,
    TurnLimits,
)
from boba.agent.queue import InputQueue, QueueRemoveReason
from boba.agent.records import PermissionMode, PermissionModeRecord, TurnOutcome
from boba.agent.session import Session, SessionError, SessionState
from boba.agent.skills import Skill, SkillTool
from boba.agent.template import AgentTemplate
from boba.agent.tools import (
    AgentTools,
    AskUserQuestionTool,
    McpServerSet,
    OwnTool,
    ResultBound,
    ToolRunner,
    ToolSearchTool,
    ToolsError,
    ToolServer,
    ToolTexts,
)
from boba.agent.turn import ContextUsage, TurnLoop
from boba.agent.writer import (
    ClientCommand,
    ClientCommands,
    HistoryWriter,
    RecordKeeper,
    RequestRecorder,
)
from boba.cancellation import RunCancellation, StopReason
from boba.llm.chat import ChatModel

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
        servers: McpServerSet | None = None,
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
        allowed_tools: Sequence[str] = (),
        denied_tools: Sequence[str] = (),
        record_requests: bool = False,
        question_tool: bool = True,
        always_load: Sequence[str] = (),
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
        self.servers = servers
        """Серверы MCP сессии: свежесть списков и инструкции на каждый ход."""
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
        self.static_rules = StaticRules(
            allowed=frozenset(allowed_tools), denied=frozenset(denied_tools)
        )
        self.record_requests = record_requests
        self.question_tool = question_tool
        """Предлагать ли модели собственный инструмент «вопрос пользователю»:
        запускающая программа без формы для вопросов его не показывает."""
        self.always_load = frozenset(always_load)
        """Инструменты, которые уходят модели полным определением с первого
        запроса; остальные инструменты серверов отложены до ToolSearch."""


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
        self._bound = ResultBound(parts.limits, parts.profile)
        self._session: Session | None = None
        self._lease: SessionLease | None = None
        self._writer: HistoryWriter | None = None
        self._queue: InputQueue | None = None
        self._turns: TurnLoop | None = None
        self._tools: AgentTools | None = None
        self._questions: ControlQuestions | None = None
        self._client_commands: ClientCommands | None = None
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
            tools = AgentTools(
                parts.tools,
                self._own_tools(),
                parts.servers,
                parts.always_load,
                wiring.templates,
                self._texts(),
            )
            self._check_tools_survived(session, tools, resume)
            self._check_profile(parts.profile, parts.system_prompt, tools)
            mode = self._effective_mode(session, resume)
        except Exception:
            await parts.store.release(lease)
            raise

        self._session = session
        self._lease = lease
        self._tools = tools
        writer = HistoryWriter(
            session, parts.store, lease, report.stored_seq, parts.sink
        )
        writer.start()
        self._writer = writer
        # починка оборванного хода ложится в журнал сразу, а не со следующим ходом
        if report.repaired:
            writer.notify()
        self._queue = InputQueue(session, wiring.clock, wiring.ids)
        self._questions = ControlQuestions(wiring.ids)
        if mode is not None:
            self._record_mode(mode)

        keeper = RecordKeeper(session, writer, parts.sink)
        runner = ToolRunner(
            tools,
            self._texts(),
            wiring.templates,
            self._questions,
            wiring.classifier,
            keeper,
            wiring.clock,
            parts.agent_name,
            parts.static_rules,
        )
        recorder = RequestRecorder(
            keeper, wiring.ids, wiring.clock, parts.record_requests
        )
        client_commands = ClientCommands(keeper, wiring.clock, parts.version)
        self._client_commands = client_commands
        attachments = ContextAttachments(
            wiring.templates, wiring.ids, wiring.clock, parts.version, session_id.value
        )
        self._turns = TurnLoop(
            keeper,
            self._queue,
            parts.profile,
            parts.limits,
            parts.compaction,
            parts.model,
            self._request_builder(parts.profile),
            attachments,
            runner,
            wiring.templates,
            wiring.ids,
            wiring.clock,
            parts.version,
            parts.sink,
            writer,
            parts.system_prompt,
            tools,
            self._skill_cards(),
            parts.environment,
            parts.agent_name,
            recorder,
            client_commands,
        )

        await parts.sink.emit(
            InitEvent(
                session_id=session_id.value,
                resumed=resume,
                model=parts.profile.model_id,
                tools=tools.names(),
                permission_mode=self.permission_mode,
                version=parts.version,
            )
        )

        return session

    def _texts(self) -> ToolTexts:
        return ToolTexts(
            self._wiring.templates,
            self._bound,
            self._parts.agent_name,
        )

    def _own_tools(self) -> Sequence[OwnTool]:
        """Собственные инструменты: вопрос пользователю, если он разрешён
        параметрами, Skill — при скиллах. ToolSearch добавляет AgentTools."""
        wiring = self._wiring
        own: list[OwnTool] = []
        if self._parts.question_tool:
            own.append(AskUserQuestionTool(wiring.templates, self._texts()))

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

        own: set[str] = {ToolSearchTool.NAME}
        for tool in self._own_tools():
            own.add(tool.name)

        offered = frozenset(tools.names()) - own
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
        SessionNotFoundError — возобновляемая сессия без журнала.
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
            raise SessionNotFoundError(msg)

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

    def set_system_prompt(self, sections: Sequence[str]) -> None:
        """Системный промпт от запускающей программы (`initialize`): принимается,
        пока агент свободен; снимок в истории он не меняет.

        Ошибки:
        SessionError — ход идёт, промпт менять поздно.
        """
        turns = self._turns
        if turns is None:
            msg = "The agent session is not open yet. Call open() before the prompt."
            raise HistoryError(msg)

        if self.session.state is not SessionState.IDLE:
            msg = (
                "The system prompt can be set only while the agent is idle; a turn "
                f"is in progress (state {self.session.state.value})."
            )
            raise SessionError(msg)

        turns.set_system_prompt(sections)

    @property
    def system_prompt(self) -> Sequence[str]:
        turns = self._turns
        if turns is None:
            return ()

        return turns.system_prompt

    def set_permission_mode(self, mode: PermissionMode) -> PermissionMode:
        """Меняет режим записью; действует со следующей проверки разрешения.

        Ошибки:
        PermissionModeError — режим недоступен при параметрах запуска.
        """
        self._wiring.ceiling.check(mode)
        self._record_mode(mode)

        return mode

    def available_modes(self) -> Sequence[PermissionMode]:
        """Режимы, которые можно включить при параметрах запуска."""
        return self._wiring.ceiling.available()

    @property
    def profile(self) -> ModelProfile:
        """Действующий профиль модели: после смены — новый."""
        turns = self._turns
        if turns is None:
            return self._parts.profile

        return turns.profile

    def set_model(self, profile: ModelProfile, model: ChatModel) -> None:
        """Переключает профиль модели для следующих ходов; принимается,
        пока агент свободен. Смену модели объявит вложение `model` в начале
        следующего хода, смену формы запроса — новый снимок промпта там же.

        Ошибки:
        SessionError — ход идёт, модель менять поздно.
        ProfileError — профиль не годится: без инструментов или промпт с
            описаниями инструментов не помещается в его окно.
        """
        turns = self._turns
        tools = self._tools
        if turns is None or tools is None:
            msg = "The agent session is not open yet. Call open() before the model."
            raise HistoryError(msg)

        if self.session.state is not SessionState.IDLE:
            msg = (
                "The model can be switched only while the agent is idle; a turn "
                f"is in progress (state {self.session.state.value})."
            )
            raise SessionError(msg)

        self._check_profile(profile, turns.system_prompt, tools)
        turns.set_model(profile, model, self._request_builder(profile))
        self._bound.set_profile(profile)

    def _request_builder(self, profile: ModelProfile) -> RequestBuilder:
        return RequestBuilder(profile)

    def _check_profile(
        self, profile: ModelProfile, system_prompt: Sequence[str], tools: AgentTools
    ) -> None:
        """Ошибки:
        ProfileError — профиль не годится для этого промпта и инструментов.
        """
        tool_texts: list[str] = []
        for tool in tools.inline():
            tool_texts.append(tool.text())

        instruction = SummaryInstruction(self._wiring.templates).text("")
        ProfileCheck(profile, self._parts.compaction, instruction).run(
            system_prompt, tool_texts
        )

    def tool_hints(self, name: str) -> ToolHints | None:
        """Пометки инструмента по имени; None — такого инструмента нет."""
        if self._tools is None:
            msg = "The agent session is not open yet. Call open() before the tools."
            raise HistoryError(msg)

        return self._tools.hints_of(name)

    def context_usage(self) -> ContextUsage:
        """Оценка заполненности контекста и окно модели."""
        turns = self._turns
        if turns is None:
            msg = "The agent session is not open yet. Call open() before the usage."
            raise HistoryError(msg)

        return turns.context_usage()

    def answer(self, request_id: str, answer: PermissionAnswer) -> bool:
        """Ответ на вопрос наружу; False — вопрос неизвестен или уже отвечен."""
        if self._questions is None:
            msg = "The agent session is not open yet. Call open() before answering."
            raise HistoryError(msg)

        return self._questions.answer(request_id, answer)

    async def record_command(self, command: ClientCommand) -> None:
        """Команда, которую клиент исполнил сам, — записью истории: вне хода
        сразу, посреди хода на ближайшей границе шага или перед его концом.

        Ошибки:
        HistoryError — сессия ещё не открыта.
        """
        client_commands = self._client_commands
        if client_commands is None:
            msg = (
                f"The agent session is not open yet, so the command "
                f"{command.text!r} is not recorded. Call open() before recording."
            )
            raise HistoryError(msg)

        await client_commands.add(command)

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
                result = await turns.run(taken, cancellation)
            finally:
                watcher.cancel()
                self._current = None
                session.enter(SessionState.IDLE)

            # конец хода слышат, когда сессия уже свободна: ответ на него
            # (смена модели, следующий вопрос) не застаёт ход идущим
            self._outcomes.append(result.outcome)
            await self._parts.sink.emit(result)
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
