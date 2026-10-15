"""Приёмник потока сессии агента для вкладки chainlit.

Живой ход вкладка-держатель публикует в шину треда: вопрос (TurnStarted),
уведомления `session/update` как есть (AgentUpdate), пометку остановки и
итог (TurnFinished) — их рисуют рендереры треда на всех инстансах одним
переводчиком UpdateView. Ключ хода — messageId, который агент дал вопросу:
получив его, держатель один раз перепривязывает к нему шаг вопроса своей
вкладки и пишет вложения вопроса. Воспроизведение `session/resume` своего
процесса рисуется тем же переводчиком прямо в вкладке, без шины: история
есть у каждого процесса агента. Побочные действия итогов вызовов — панель
канваса, вложения — исполняет только держатель хода (ResultMounts).

Ошибки:
LockBusyError — тред уже ведёт другой держатель: ход не начат.
MessageBusError — шина не приняла сообщение держателя.
McpClientError — серверы MCP профиля не открылись для вида аргументов.
DataUnavailableError — оценки или элементы треда не прочитаны слоем данных.
InternalServiceError — шага вопроса нет в контексте вкладки.
AcpClientError — агент не принял вопрос.
"""

from __future__ import annotations

import asyncio
import logging
from abc import abstractmethod
from collections.abc import Coroutine, Iterator, Sequence
from contextlib import AsyncExitStack, contextmanager
from dataclasses import dataclass, replace
from typing import Any, ClassVar, Protocol

from acp.experimental.v2 import schema

import chainlit as cl
from boba.cancellation import RunCancellation
from boba.canvas.canvas import CanvasError, CanvasErrorKind
from boba.canvas.keys import ElementProps, ObjectKey
from boba.chainlit.agent.chat import (
    PermissionKind,
    Question,
    SessionOptions,
    ThreadIdentity,
    TurnRenderer,
    TurnRenderers,
)
from boba.chainlit.agent.client import SessionUpdate
from boba.chainlit.canvas.panel import CanvasPanel
from boba.chainlit.canvas.remote import RemoteJournals
from boba.chainlit.chat.feed import TurnFeed
from boba.chainlit.data.data_layer import AttachmentDataLayer, FeedbackRows
from boba.chainlit.domain.fields import StepField
from boba.chainlit.rendering.acp_view import ToolMedia, UpdateView
from boba.chainlit.rendering.chat_view import ChatView, LiveSink, StepRole, StepText
from boba.chainlit.rendering.renderer import RenderSurface
from boba.chainlit.rendering.tool import ChatElements, WorkspaceFile
from boba.identity.context import Scope
from boba.identity.errors import InternalServiceError
from boba.identity.locks import LiveLocks, LockKeeper, LockMode, LockPurpose
from boba.mcp_client.client import McpCaller, McpServers
from boba.messaging import MessageBus, NoticeLevel, PayloadStore, TurnOutcome
from boba.toolkit.result import FileElement, PanelOpen, ToolResultBase
from boba.transport.acp.protocol import IdleErrorUpdate, StopReason, ToolStatus
from chainlit.context import ChainlitContext, context_var
from chainlit.emitter import ChainlitEmitter
from chainlit.session import WebsocketSession
from chainlit.step import StepDict

__all__ = [
    "AgentFeed",
    "AgentFeeds",
    "BusParts",
    "ResultMounts",
    "TabEmitter",
    "TabSurface",
    "TabSurfaces",
]

logger = logging.getLogger(__name__)


class TabSurfaces(Protocol):
    """Откуда у приёмника вкладка и поверхность рассылки треда: сокет текущего
    обработчика и контекст с эмиттером по всем вкладкам треда. Реализует
    ThreadSurfaces (infra.thread_room)."""

    @abstractmethod
    def anchor(self) -> WebsocketSession:
        """Сокет текущего обработчика chainlit.

        Ошибки:
        InternalServiceError — у текущей сессии нет сокета.
        """

    @abstractmethod
    def thread(self, anchor: WebsocketSession, thread_id: str) -> RenderSurface:
        """Поверхность рассылки по всем вкладкам треда с якорной сессией."""


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


class ResultMounts:
    """Побочные действия итога вызова на поверхности чата: открыть файл в
    панели канваса и оставить ссылку в переписке (PanelOpen), прикрепить файл
    workspace вложением (FileElement). Исполняет держатель хода по итогу из
    `rawOutput`: элемент пишется в слой данных и уходит вкладкам через шину.
    Создаётся приёмником потока на тред и видит все уведомления живого хода:
    имя инструмента агент называет только в первом уведомлении вызова, и
    его запоминает здесь же, а итог из `rawOutput` оживляет переводчик
    приёмника (UpdateView.outcome).
    """

    def __init__(
        self, thread_id: str, user_key: str, surface: RenderSurface, view: UpdateView
    ) -> None:
        self._thread_id = thread_id
        self._user_key = user_key
        self._surface = surface
        self._view = view
        self._called: dict[str, str] = {}
        """Идущий вызов живого хода → имя его инструмента."""

    async def mounted(
        self, update: SessionUpdate, turn_key: str, feed: TurnFeed
    ) -> None:
        """Действия по итогу вызова; уведомление не итог — ничего."""
        if not isinstance(update, schema.SessionToolCallUpdate):
            return

        call_id = update.tool_call_id
        if update.name:
            self._called[call_id] = update.name

        if update.status is None:
            return

        if not ToolStatus(update.status).terminal:
            return

        name = self._called.pop(call_id, None)
        if name is None:
            return

        outcome = self._view.outcome(update, name)
        if outcome is None:
            return

        artifact = outcome.artifact

        context = self._surface.context()
        if context is None:
            return

        token = context_var.set(context)
        try:
            await self._mount(artifact, update.tool_call_id, turn_key, feed)
        finally:
            context_var.reset(token)

    async def _mount(
        self, artifact: ToolResultBase, call_id: str, turn_key: str, feed: TurnFeed
    ) -> None:
        for item in artifact.chat_view().items:
            if isinstance(item, PanelOpen):
                await self._panel(item, call_id, turn_key, feed)

            if isinstance(item, FileElement):
                await self._file(item, call_id, turn_key, feed)

    async def _panel(
        self, item: PanelOpen, call_id: str, turn_key: str, feed: TurnFeed
    ) -> None:
        """Панель с файлом плюс ссылка на него в переписке; отказ панели —
        пометка в ленте: итог у агента уже записан."""
        try:
            key = self._key(item.path)
            opened = await CanvasPanel.open(key)
        except CanvasError as exc:
            note = f"canvas refused {item.path!r} of call {call_id}: {exc}"
            logger.info("thread %s: %s", self._thread_id, note)
            await feed.notice(NoticeLevel.ERROR, note)
            return

        element = ChatElements.of_result(opened.link)
        await self._publish(element, call_id, turn_key, feed)

    async def _file(
        self, item: FileElement, call_id: str, turn_key: str, feed: TurnFeed
    ) -> None:
        try:
            key = self._key(item.path)
        except CanvasError as exc:
            note = f"attachment {item.path!r} of call {call_id} is refused: {exc}"
            logger.info("thread %s: %s", self._thread_id, note)
            await feed.notice(NoticeLevel.ERROR, note)
            return

        element_id = self._element_id(call_id)
        links = AttachmentDataLayer.require().links
        element = WorkspaceFile(
            id=element_id,
            name=item.name,
            thread_id=self._thread_id,
            url=links.url(self._thread_id, element_id, key.dir),
            mime=item.mime,
            display="inline",
            props=ElementProps(dir=key.dir).model_dump(mode="json"),
        )
        await self._publish(element, call_id, turn_key, feed)

    async def _publish(
        self, element: cl.Element, call_id: str, turn_key: str, feed: TurnFeed
    ) -> None:
        """Строка в elements и показ через шину хода: элемент рисуют вкладки
        треда на всех инстансах и он переживает перезагрузку треда."""
        element.id = self._element_id(call_id)
        element.thread_id = self._thread_id
        element.for_id = ChatView.derive_id(self._thread_id, turn_key, StepRole.ANSWER)
        await AttachmentDataLayer.require().create_element(element)
        await feed.element_shown(call_id, element.to_dict())

    def _element_id(self, call_id: str) -> str:
        element_id = ChatView.derive_id(self._thread_id, call_id, StepRole.ELEMENT)
        if element_id is None:
            msg = (
                f"element of call {call_id!r} of thread {self._thread_id}: "
                "the call id is empty"
            )
            raise ValueError(msg)

        return element_id

    def _key(self, path: str) -> ObjectKey:
        """Ключ файла в области треда; путь вне каталогов треда — отказ."""
        try:
            return ObjectKey.from_workspace(self._user_key, self._thread_id, path)
        except ValueError as exc:
            raise CanvasError(CanvasErrorKind.BAD_PATH, str(exc)) from exc


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
        schema.SessionNotice,
        schema.RunningSessionStateUpdate,
        schema.RequiresActionSessionStateUpdate,
        schema.IdleSessionStateUpdate,
    )
    """Уведомления сессии, которых в ленте нет: заголовок, план, команды,
    сжатие, пометки и состояние хода (его конец приходит turn_finished)."""

    def __init__(  # noqa: PLR0913 — приёмник собирается всеми своими входами
        self,
        identity: ThreadIdentity,
        options: SessionOptions,
        replay: UpdateView,
        tab: TabSurface,
        surface: RenderSurface,
        parts: BusParts,
        feedbacks: FeedbackRows,
    ) -> None:
        self._identity = identity
        self._options = options
        self._replay = replay
        self._tab = tab
        self._surface = surface
        self._parts = parts
        self._feedbacks = feedbacks
        self._mounts = ResultMounts(
            identity.thread_id, str(identity.user_id), surface, replay
        )
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
        self, session_id: str, question: Question, prompt: Coroutine[Any, Any, str]
    ) -> str:
        """Замок области треда, затем вопрос агенту; уведомления хода, пришедшие
        до ответа, ждут в очереди. Ответ — messageId вопроса: шаг вопроса
        перепривязывается к нему, ход уходит в шину под этим ключом.

        Ошибки:
        LockBusyError — тред уже ведёт другой держатель.
        AcpClientError — агент не принял вопрос.
        MessageBusError — шина не приняла TurnStarted.
        InternalServiceError — шага вопроса нет в контексте вкладки.
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
            prompt.close()
            raise

        cancellation = RunCancellation()
        keeper = LockKeeper(parts.locks, lock, cancellation, parts.heartbeat_sec)
        async with self._ordered, AsyncExitStack() as stack:
            await stack.enter_async_context(keeper)
            key = await prompt
            rebound = await self._rebind(question, key)
            feed = TurnFeed(parts.bus, parts.payloads, self._scope, key, lock.token)
            await feed.started(key, rebound.body(self._identity.login))
            self._turn = LiveTurn(
                key=key, feed=feed, held=stack.pop_all(), cancellation=cancellation
            )

        logger.info(
            "thread %s: turn %s of session %s is published to the bus",
            self.thread_id,
            key,
            session_id,
        )

        return key

    async def _rebind(self, question: Question, key: str) -> Question:
        """Шаг вопроса вкладки получает id, который вопросу дал агент: шаг
        с прежним id убирается, сообщение в контексте chainlit меняет id и
        отправляется заново без вложений, вложения вопроса держатель пишет
        сам под новым id. Итог — вопрос с ключом key.

        Ошибки:
        InternalServiceError — сообщения вопроса нет в контексте вкладки.
        DataUnavailableError — вложение не записано.
        """
        found: cl.Message | None = None
        for message in cl.chat_context.get():
            if message.id == question.key:
                found = message

        if found is None:
            msg = (
                f"thread {self.thread_id}: the question step {question.key} is not "
                f"in the chat context of the tab, so it cannot take the id {key}"
            )
            raise InternalServiceError(internal_detail=msg, user_detail=None)

        await found.remove()
        found.id = key
        layer = AttachmentDataLayer.require()
        rebound = Question.of_message(found, self.thread_id, layer.links)
        attached = found.elements
        found.elements = []
        await found.send()
        found.elements = attached
        for element in attached:
            copied = replace(
                element, for_id=key, thread_id=self.thread_id, chainlit_key=None
            )
            await layer.create_element(copied)

        return rebound

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

        if isinstance(update, self._SERVICE_UPDATES):
            return

        turn = self._turn
        if turn is None:
            with self._tab.entered():
                await self._replay.apply(update)

            return

        await turn.feed.agent_update(update)
        await self._mounts.mounted(update, turn.key, turn.feed)

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
                note = StepText.STOPPED.value
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
                await turn.feed.finished(TurnOutcome.FAILED, StepText.TURN_FAILED.value)
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
        rows = await self._feedbacks.feedbacks_of(self.thread_id)
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
        feedbacks: FeedbackRows,
        surfaces: TabSurfaces,
    ) -> None:
        self._parts = parts
        self._journals = journals
        self._media = media
        self._servers = servers
        self._feedbacks = feedbacks
        self._surfaces = surfaces

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
            self._media.shown(name, port.tools(), port.nested_calls())

        self._journals.mark_streamable(self._servers.journaled())

        anchor = self._surfaces.anchor()
        view = ChatView(
            identity.thread_id, LiveSink(), self._journals, user_name=identity.login
        )
        replay = UpdateView(view, identity.thread_id, self._journals, self._media)

        return AgentFeed(
            identity,
            options,
            replay,
            TabSurface(anchor),
            self._surfaces.thread(anchor, identity.thread_id),
            self._parts,
            self._feedbacks,
        )
