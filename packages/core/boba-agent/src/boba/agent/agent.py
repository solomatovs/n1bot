"""Агент: связывает сессию, очередь, писатель, загрузчик и цикл хода.

Один объект на процесс и на сессию. `open` захватывает сессию в хранилище,
при возобновлении восстанавливает её загрузчиком, проверяет профиль и
запускает писатель; `serve` ждёт очередь и ведёт ходы один за другим;
`interrupt` останавливает текущий ход; `close` гасит ход, сбрасывает журнал
и отпускает сессию. Переходы состояний — план, раздел 5.18.

Ошибки:
HistoryError — хранилище недоступно, сессия занята или журнал для
    возобновления пуст.
ProfileError — профиль модели не годится для запуска.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from typing import ClassVar

from boba.agent.attachments import ContextAttachments, SnapshotTool
from boba.agent.builder import RequestBuilder
from boba.agent.events import EventSink, InitEvent, TurnOutcome
from boba.agent.history import HistoryError, HistoryStore, SessionId, SessionLease
from boba.agent.ids import Clock, IdMint, SystemClock
from boba.agent.loader import HistoryLoader, LoadReport
from boba.agent.profile import (
    CompactionSettings,
    ModelProfile,
    ProfileCheck,
    TurnLimits,
)
from boba.agent.queue import InputQueue, QueueRemoveReason
from boba.agent.records import PermissionMode, PermissionModeRecord
from boba.agent.session import Session, SessionState
from boba.agent.template import AgentTemplate
from boba.agent.turn import TurnLoop
from boba.agent.writer import HistoryWriter
from boba.cancellation import RunCancellation, StopReason
from boba.llm.chat import ChatModel

logger = logging.getLogger(__name__)

__all__ = ["Agent", "AgentParts"]


class AgentParts:
    """Зависимости агента, не привязанные к сессии."""

    def __init__(  # noqa: PLR0913 — зависимости агента перечислены все разом
        self,
        profile: ModelProfile,
        limits: TurnLimits,
        compaction: CompactionSettings,
        system_prompt: Sequence[str],
        tools: Sequence[SnapshotTool],
        store: HistoryStore,
        model: ChatModel,
        sink: EventSink,
        version: str,
        initial_mode: PermissionMode = PermissionMode.DEFAULT,
        templates: AgentTemplate | None = None,
        ids: IdMint | None = None,
        clock: Clock | None = None,
    ) -> None:
        self.profile = profile
        self.limits = limits
        self.compaction = compaction
        self.system_prompt = list(system_prompt)
        self.tools = list(tools)
        self.store = store
        self.model = model
        self.sink = sink
        self.version = version
        self.initial_mode = initial_mode
        self.templates = templates
        self.ids = ids
        self.clock = clock


class Agent:
    """Процесс одной сессии: открыть, обслуживать очередь, остановить."""

    LOADER_LOG: ClassVar[str] = (
        "agent %s: resumed %d record(s), %d repaired, %d skipped"
    )

    def __init__(self, parts: AgentParts) -> None:
        self._parts = parts
        self._templates = parts.templates
        if self._templates is None:
            self._templates = AgentTemplate()

        self._ids = parts.ids
        if self._ids is None:
            self._ids = IdMint()

        self._clock = parts.clock
        if self._clock is None:
            self._clock = SystemClock()

        self._session: Session | None = None
        self._lease: SessionLease | None = None
        self._writer: HistoryWriter | None = None
        self._queue: InputQueue | None = None
        self._turns: TurnLoop | None = None
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

    async def open(self, session_id: SessionId, resume: bool) -> Session:
        parts = self._parts
        templates = self._templates
        ids = self._ids
        clock = self._clock
        if templates is None or ids is None or clock is None:
            msg = "The agent has no templates, id mint or clock. This is a wiring bug."
            raise HistoryError(msg)

        lease = await parts.store.acquire(session_id)
        session = Session(session_id)
        report = LoadReport()
        try:
            if resume:
                loader = HistoryLoader(
                    parts.store, templates, ids, clock, parts.version
                )
                report = await loader.load(session)
                if session.last_seq == 0:
                    msg = (
                        f"Cannot resume session '{session_id.value}': there is no "
                        "journal to resume. Start a new session, or check that the "
                        "history root points to the right place."
                    )
                    raise HistoryError(msg)

                logger.info(
                    self.LOADER_LOG,
                    session_id.value,
                    report.stored_seq,
                    len(report.repaired),
                    report.skipped_lines + report.skipped_records,
                )

            tool_texts: list[str] = []
            for tool in parts.tools:
                tool_texts.append(tool.text())

            ProfileCheck(parts.profile, parts.compaction).run(
                parts.system_prompt, tool_texts
            )
        except Exception:
            await parts.store.release(lease)
            raise

        self._session = session
        self._lease = lease
        writer = HistoryWriter(session, parts.store, lease, report.stored_seq)
        writer.start()
        self._writer = writer
        self._queue = InputQueue(session, clock, ids)

        if not resume:
            session.add(
                PermissionModeRecord(
                    timestamp=clock.now(),
                    session_id=session_id.value,
                    permission_mode=parts.initial_mode,
                )
            )
            writer.notify()

        attachments = ContextAttachments(templates, ids, clock, parts.version)
        self._turns = TurnLoop(
            session,
            parts.profile,
            parts.limits,
            parts.model,
            RequestBuilder(parts.profile, templates),
            attachments,
            templates,
            ids,
            clock,
            parts.version,
            parts.sink,
            writer,
            parts.system_prompt,
            parts.tools,
        )

        tool_names: list[str] = []
        for tool in parts.tools:
            tool_names.append(tool.name)

        await parts.sink.emit(
            InitEvent(
                session_id=session_id.value,
                resumed=resume,
                model=parts.profile.model_id,
                tools=tool_names,
                permission_mode=parts.initial_mode,
                version=parts.version,
            )
        )

        return session

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
            try:
                outcome = await turns.run(taken, cancellation)
            finally:
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
