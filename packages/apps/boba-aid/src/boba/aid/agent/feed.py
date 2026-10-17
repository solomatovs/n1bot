"""Приёмник потока сессии агента для вкладки chainlit.

Живой ход вкладка-держатель публикует в шину треда: вопрос (TurnStarted),
уведомления `session/update` как есть (AgentUpdate), пометку остановки и
итог (TurnFinished) — их рисуют рендереры треда на всех инстансах одним
переводчиком UpdateView. Ключ хода — messageId, который агент дал вопросу:
получив его, держатель один раз снимает в своей вкладке шаг вопроса с
прежним id; вопрос с вложениями всем вкладкам рисует рендерер треда по
TurnStarted. Воспроизведение `session/resume` своего процесса рисуется тем
же переводчиком прямо в вкладке, без шины: история есть у каждого процесса
агента. Файлы и ссылки итогов рисует лента.

Ошибки:
LockBusyError — тред уже ведёт другой держатель: ход не начат.
MessageBusError — шина не приняла сообщение держателя.
McpClientError — серверы MCP профиля не открылись для вида аргументов.
DataUnavailableError — тред или оценки не прочитаны слоем данных.
InternalServiceError — треда нет в слое данных.
AcpClientError — агент не принял вопрос.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Coroutine, Iterator, Sequence
from contextlib import AsyncExitStack, contextmanager
from dataclasses import dataclass
from typing import Any, ClassVar

from acp.experimental.v2 import schema
from chainlit.context import ChainlitContext, context, context_var
from chainlit.emitter import ChainlitEmitter
from chainlit.session import WebsocketSession
from chainlit.step import StepDict

from boba.aid.agent.chat import (
    PermissionKind,
    Question,
    SessionOptions,
    StartedTurn,
    ThreadIdentity,
    TurnRenderer,
    TurnRenderers,
)
from boba.aid.canvas.remote import RemoteJournals
from boba.aid.chat.feed import TurnFeed
from boba.aid.chat.ui_text import UiText
from boba.aid.data.data_layer import FeedRows
from boba.aid.domain.fields import StepField
from boba.aid.rendering.acp_view import SessionUpdate, ToolMedia, UpdateView
from boba.aid.rendering.chat_view import ChatView, LiveSink, StepText
from boba.aid.rendering.errors import show_error
from boba.aid.rendering.renderer import RenderSurface, TabSurfaces
from boba.cancellation import RunCancellation
from boba.identity.context import Scope
from boba.identity.errors import InternalServiceError
from boba.identity.locks import LiveLocks, LockKeeper, LockMode, LockPurpose
from boba.mcp_client.client import McpCaller, McpServers
from boba.messaging import MessageBus, NoticeLevel, PayloadStore, TurnOutcome
from boba.runtime.storage import StorageClient
from boba.transport.acp.protocol import IdleErrorUpdate, StopReason

__all__ = [
    "AgentFeed",
    "AgentFeeds",
    "BusParts",
    "TabEmitter",
    "TabSurface",
]

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class BusParts:
    """Шина инстанса, какой её видит держатель хода: сообщения, тела,
    блокировки областей и период подтверждения жизни."""

    bus: MessageBus
    payloads: PayloadStore
    locks: LiveLocks
    heartbeat_sec: float


class TabEmitter(ChainlitEmitter):
    """Эмиттер одной вкладки, который помнит последнюю отправку каждого шага
    (в том числе стримящихся сообщений, идущих мимо sink'а): по ним после
    воспроизведения к шагам возвращаются оценки."""

    def __init__(self, session: WebsocketSession) -> None:
        super().__init__(session)
        self._sent: dict[str, StepDict] = {}

    def sent(self, step_id: str) -> StepDict | None:
        """Последняя отправка шага; None — шаг не рисовался."""
        return self._sent.get(step_id)

    def _remember(self, step_dict: StepDict) -> None:
        step_id = step_dict.get(StepField.ID)
        if step_id:
            self._sent[step_id] = step_dict

    def send_step(self, step_dict: StepDict) -> Coroutine[Any, Any, None]:
        self._remember(step_dict)

        return super().send_step(step_dict)

    def update_step(self, step_dict: StepDict) -> Coroutine[Any, Any, None]:
        self._remember(step_dict)

        return super().update_step(step_dict)

    def stream_start(self, step_dict: StepDict) -> Coroutine[Any, Any, None]:
        self._remember(step_dict)

        return super().stream_start(step_dict)


class TabSurface:
    """Контекст chainlit одной вкладки: воспроизведение истории рисуется
    только ей, остальные вкладки треда воспроизводят её сами."""

    def __init__(self, anchor: WebsocketSession) -> None:
        self._anchor = anchor
        self._emitter = TabEmitter(anchor)

    @property
    def emitter(self) -> TabEmitter:
        return self._emitter

    @contextmanager
    def entered(self) -> Iterator[None]:
        token = context_var.set(ChainlitContext(self._anchor, self._emitter))
        try:
            yield
        finally:
            context_var.reset(token)


@dataclass
class LiveTurn:
    """Идущий ход держателя: его производитель, ключ вопроса и замок области
    (held: выход из стека отпускает замок)."""

    key: str
    feed: TurnFeed
    held: AsyncExitStack
    cancellation: RunCancellation


class AgentFeed(TurnRenderer):
    """Реализация TurnRenderer для агента одного треда во вкладке.

    Живой ход (между turn_started и turn_finished) публикует уведомления в
    шину под замком области треда; остальные уведомления — воспроизведение
    `session/resume` — рисуются переводчиком вкладки. Настройки сессии из
    уведомлений ложатся в общее с агентом состояние SessionOptions.
    Клиент SDK обрабатывает каждое уведомление своей задачей, поэтому
    приёмник выстраивает их в порядок прихода очередью (asyncio.Lock
    отдаёт замок ждущим по порядку); конец хода и конец воспроизведения
    встают в ту же очередь после последнего уведомления. Создаётся
    фабрикой AgentFeeds.
    """

    _SERVICE_UPDATES: ClassVar[tuple[type[SessionUpdate], ...]] = (
        schema.SessionInfoUpdate,
        schema.SessionPlanUpdate,
        schema.SessionPlanRemovedUpdate,
        schema.AvailableCommandsUpdate,
        schema.SessionCompactionUpdate,
        schema.SessionCompactionSummaryChunk,
        schema.RunningSessionStateUpdate,
        schema.RequiresActionSessionStateUpdate,
        schema.IdleSessionStateUpdate,
    )
    """Уведомления сессии, которых в ленте нет: заголовок, план, команды,
    сжатие и состояние хода (его конец приходит turn_finished)."""

    ABANDONED: ClassVar[str] = "turn failed"
    """Причина в итоге хода, сорвавшегося без ответа агента."""

    def __init__(  # noqa: PLR0913 — приёмник собирается всеми своими входами
        self,
        identity: ThreadIdentity,
        options: SessionOptions,
        replay: UpdateView,
        tab: TabSurface,
        surface: RenderSurface,
        parts: BusParts,
        rows: FeedRows,
    ) -> None:
        self._identity = identity
        self._options = options
        self._replay = replay
        self._tab = tab
        self._surface = surface
        self._parts = parts
        self._rows = rows
        self._scope = Scope.chat(identity.thread_id)
        self._turn: LiveTurn | None = None
        self._stopping: set[str] = set()
        self._ordered = asyncio.Lock()

    @property
    def thread_id(self) -> str:
        return self._identity.thread_id

    def stopping(self, session_id: str) -> None:
        self._stopping.add(session_id)

    async def turn_started(
        self, question: Question, start: Coroutine[Any, Any, StartedTurn]
    ) -> StartedTurn:
        """Замок области треда, затем начало хода: сессия (ветка при правке
        идёт уже под замком) и вопрос агенту; уведомления хода, пришедшие до
        ответа, ждут в очереди. Ключ хода — messageId вопроса: шаг вопроса
        перепривязывается к нему, ход уходит в шину под этим ключом.

        Ошибки:
        LockBusyError — тред уже ведёт другой держатель.
        AcpClientError — агент не принял вопрос.
        MessageBusError — шина не приняла TurnStarted.
        """
        parts = self._parts
        try:
            lock = await parts.locks.acquire(
                self._scope,
                LockMode.EXCLUSIVE,
                LockPurpose.TURN,
                self._identity.user_id,
            )
        except Exception:
            start.close()
            raise

        cancellation = RunCancellation()
        keeper = LockKeeper(parts.locks, lock, cancellation, parts.heartbeat_sec)
        async with self._ordered, AsyncExitStack() as stack:
            await stack.enter_async_context(keeper)
            turn = await start
            key = turn.key
            rebound = await self._rebind(question, key)
            feed = TurnFeed(parts.bus, parts.payloads, self._scope, key, lock.token)
            # другие вкладки перечитывают ветку раньше, чем увидят новый ход
            if question.edited:
                await feed.rewound(turn.session_id)

            await feed.started(key, rebound.body(self._identity.login))
            self._turn = LiveTurn(
                key=key, feed=feed, held=stack.pop_all(), cancellation=cancellation
            )

        logger.info(
            "thread %s: turn %s of session %s is published to the bus",
            self.thread_id,
            key,
            turn.session_id,
        )

        return turn

    async def _rebind(self, question: Question, key: str) -> Question:
        """Вопрос получает id, который ему дал агент: шаг с прежним id
        (оптимистичный шаг нового вопроса или правленый шаг) снимается во
        вкладке держателя: у других вкладок оптимистичного шага нет, а после
        правки они перерисовываются по ThreadRewound.
        Итог — вопрос с ключом key; сам вопрос с вложениями всем вкладкам
        рисует рендерер треда по TurnStarted."""
        await context.emitter.delete_step(StepDict(id=question.key))

        return question.model_copy(update={"key": key})

    async def cleared(self) -> None:
        """Тред без шагов уходит только этой вкладке: фронт заменяет ленту
        пустой, и следующее воспроизведение рисует её заново."""
        async with self._ordered:
            thread = await self._rows.get_thread(self.thread_id)
            if thread is None:
                msg = (
                    f"clearing the feed of thread {self.thread_id}: the data layer "
                    "has no such thread"
                )
                raise InternalServiceError(internal_detail=msg, user_detail=None)

            await self._tab.emitter.resume_thread(thread)

    async def update(self, session_id: str, update: SessionUpdate) -> None:
        async with self._ordered:
            await self._update(update)

    async def _update(self, update: SessionUpdate) -> None:
        if isinstance(update, schema.ConfigOptionUpdate):
            self._options.replace(update.config_options)
            return

        if isinstance(update, schema.UsageUpdate):
            logger.debug(
                "thread %s: context %d of %d tokens",
                self.thread_id,
                update.used,
                update.size,
            )
            return

        if isinstance(update, schema.SessionNotice):
            await self._notice(update)
            return

        if isinstance(update, self._SERVICE_UPDATES):
            return

        turn = self._turn
        if turn is None:
            with self._tab.entered():
                await self._replay.apply(update)

            return

        await turn.feed.agent_update(update)

    async def _notice(self, notice: schema.SessionNotice) -> None:
        """Пометка агента: в идущем ходе — уведомлением ленты хода, вне хода
        ошибка — сообщением вкладке, остальное — в журнал процесса."""
        text = notice.title
        if notice.description:
            text = f"{notice.title}: {notice.description}"

        level = self._level(notice.severity)
        turn = self._turn
        if turn is not None:
            await turn.feed.notice(level, text)
            return

        if level is not NoticeLevel.ERROR:
            logger.info("thread %s: agent notice: %s", self.thread_id, text)
            return

        with self._tab.entered():
            await show_error(f"agent of thread {self.thread_id}: {text}", "Agent")

    @staticmethod
    def _level(severity: str) -> NoticeLevel:
        """Важность пометки: severity ACP — открытый набор, незнакомое значение
        показывается как сведение."""
        for level in NoticeLevel:
            if level.value == severity:
                return level

        return NoticeLevel.INFO

    async def permission(
        self, session_id: str, title: str, options: Sequence[schema.PermissionOption]
    ) -> schema.RequestPermissionResponse:
        """Режим без вопросов: первый вариант «разрешить»; при остановке — отмена."""
        if session_id in self._stopping:
            return schema.RequestPermissionResponse(
                outcome=schema.CancelledPermissionOutcome()
            )

        chosen = self._allowing(options)
        logger.info(
            "thread %s: permission for %r answered with %s",
            self.thread_id,
            title,
            chosen.option_id,
        )

        return schema.RequestPermissionResponse(
            outcome=schema.SelectedPermissionOutcomeVariant(option_id=chosen.option_id)
        )

    async def turn_finished(
        self, session_id: str, idle: schema.IdleSessionStateUpdate
    ) -> None:
        """Итог хода в шину: штатный конец, остановка с пометкой либо ранний
        конец с текстом причины; замок области отпускается."""
        self._stopping.discard(session_id)
        async with self._ordered:
            await self._finish(idle)

    async def _finish(self, idle: schema.IdleSessionStateUpdate) -> None:
        turn = self._turn
        if turn is None:
            return

        stop_reason = idle.stop_reason
        try:
            ended = stop_reason == StopReason.END_TURN
            unexplained = stop_reason is None
            if ended or unexplained:
                await turn.feed.finished(TurnOutcome.OK, "")
                return

            if stop_reason == StopReason.CANCELLED:
                note = self._replay.view.label(StepText.STOPPED)
                await turn.feed.answer_interrupted(turn.key, note)
                await turn.feed.finished(TurnOutcome.STOPPED, note)
                return

            note = f"the turn ended early: {stop_reason}"
            failure = None
            if isinstance(idle, IdleErrorUpdate):
                failure = idle.error

            if failure is not None:
                note = f"the turn failed: {failure.message}"

            await turn.feed.notice(NoticeLevel.ERROR, note)
            await turn.feed.finished(TurnOutcome.FAILED, note)
        finally:
            await self._release(turn)

    async def abandon(self, session_id: str) -> None:
        """Ход сорвался без ответа агента: ленты закрывают его как сбой."""
        self._stopping.discard(session_id)
        async with self._ordered:
            turn = self._turn
            if turn is None:
                return

            try:
                await turn.feed.finished(TurnOutcome.FAILED, self.ABANDONED)
            finally:
                await self._release(turn)

    async def loaded(self, session_id: str) -> None:
        """Воспроизведение закончено: последний ход закрывается, к шагам
        возвращаются сохранённые оценки. Отправленное переводчик забывает:
        вкладка, вернувшаяся к треду ещё раз (обрыв сокета, смена профиля),
        получает пустую ленту, и следующее воспроизведение обязано отправить
        шаги заново, а не обновлениями к шагам, которых у неё уже нет."""
        async with self._ordered:
            with self._tab.entered():
                if self._replay.turn_key is not None:
                    await self._replay.finish_turn()

                await self._restore_feedback()

            self._replay.view.rebuilt()

    async def _restore_feedback(self) -> None:
        rows = await self._rows.feedbacks_of(self.thread_id)
        emitter = self._tab.emitter
        for step_id, feedback in rows.items():
            step = emitter.sent(step_id)
            if step is None:
                continue

            rated = step.copy()
            rated[StepField.FEEDBACK] = feedback
            await emitter.update_step(rated)

    async def _release(self, turn: LiveTurn) -> None:
        self._turn = None
        await turn.held.aclose()

    @staticmethod
    def _allowing(
        options: Sequence[schema.PermissionOption],
    ) -> schema.PermissionOption:
        """Первый вариант «разрешить»; без него — первый из предложенных."""
        for option in options:
            if option.kind == PermissionKind.ALLOW_ONCE:
                return option

        for option in options:
            if option.kind == PermissionKind.ALLOW_ALWAYS:
                return option

        return options[0]


class AgentFeeds(TurnRenderers):
    """Фабрика приёмников потока: на агента треда — переводчик воспроизведения
    во вкладке, поверхность рассылки треда и шина инстанса. Карточки
    инструментов серверов профиля (вид аргументов, журнал) узнаются здесь же
    у клиента MCP чата. Создаётся провайдером процесса.
    """

    def __init__(  # noqa: PLR0913 — фабрика собирается всеми входами процесса
        self,
        parts: BusParts,
        journals: RemoteJournals,
        media: ToolMedia,
        servers: McpServers,
        rows: FeedRows,
        surfaces: TabSurfaces,
        storage: StorageClient,
        texts: UiText,
    ) -> None:
        self._parts = parts
        self._texts = texts
        self._journals = journals
        self._media = media
        self._servers = servers
        self._rows = rows
        self._surfaces = surfaces
        self._storage = storage

    async def create(
        self, identity: ThreadIdentity, options: SessionOptions
    ) -> AgentFeed:
        """Ошибки:
        McpClientError — сервер профиля не объявлен или не открылся.
        InternalServiceError — у текущей сессии chainlit нет сокета.
        """
        caller = McpCaller(login=identity.login, roles=identity.roles)
        for name in identity.profile.config.mcp:
            port = await self._servers.port(name, caller)
            self._media.shown(
                name,
                port.tools(),
                port.nested_calls(),
                port.journal_links(),
                port.apps(),
            )

        anchor = self._surfaces.anchor()
        view = ChatView(
            identity.thread_id,
            LiveSink(),
            str(identity.user_id),
            self._storage,
            self._texts,
            user_name=identity.login,
        )
        replay = UpdateView(view, identity.thread_id, self._journals, self._media)

        return AgentFeed(
            identity,
            options,
            replay,
            TabSurface(anchor),
            self._surfaces.thread(anchor, identity.thread_id),
            self._parts,
            self._rows,
        )
