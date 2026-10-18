"""Приёмник потока сессии агента для вкладки chainlit.

Живой ход вкладка-держатель публикует в шину треда: вопрос (TurnStarted),
уведомления `session/update` как есть (AgentUpdate), пометку остановки и
итог (TurnFinished) — их рисуют рендереры треда на всех инстансах одним
переводчиком UpdateView. Ключ хода — messageId, который агент дал вопросу:
получив его, держатель один раз снимает в своей вкладке шаг вопроса с
прежним id; вопрос с вложениями всем вкладкам рисует рендерер треда по
TurnStarted. Воспроизведение `session/resume` своего процесса рисуется тем
же переводчиком прямо в вкладке, без шины: история есть у каждого процесса
агента. Файлы и ссылки итогов рисует лента. Вопрос агента пользователю
(форма `elicitation/create`) держатель показывает в своей вкладке панелью
вместо поля ввода (элемент Question и `ask` chainlit) до ответа, срока
[agent].question_timeout_sec или снятия; вкладка, подключившаяся к треду
заново, получает висящий вопрос снова.

Ошибки:
LockBusyError — тред уже ведёт другой держатель: ход не начат.
MessageBusError — шина не приняла сообщение держателя.
DataUnavailableError — тред или оценки не прочитаны слоем данных.
InternalServiceError — треда нет в слое данных.
AcpClientError — агент не принял вопрос.
"""

from __future__ import annotations

import asyncio
import logging
import math
from collections.abc import (
    Awaitable,
    Collection,
    Coroutine,
    Iterator,
    Mapping,
    Sequence,
)
from contextlib import AsyncExitStack, contextmanager
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, ClassVar, Literal, cast
from uuid import uuid4

from acp.exceptions import RequestError
from acp.experimental.v2 import schema
from chainlit.context import ChainlitContext, context, context_var
from chainlit.element import CustomElement
from chainlit.emitter import ChainlitEmitter
from chainlit.session import WebsocketSession
from chainlit.step import StepDict
from chainlit.types import AskElementSpec
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from boba.aid.agent.chat import (
    PermissionKind,
    Question,
    SessionOptions,
    StartedTurn,
    ThreadIdentity,
    TurnRenderer,
    TurnRenderers,
)
from boba.aid.agent.client import ElicitationReply
from boba.aid.canvas.remote import RemoteJournals
from boba.aid.chat.feed import TurnFeed
from boba.aid.chat.ui_text import UiText
from boba.aid.data.data_layer import FeedRows
from boba.aid.domain.fields import StepField
from boba.aid.rendering.acp_view import (
    ServerCards,
    SessionUpdate,
    ToolMedia,
    UpdateView,
)
from boba.aid.rendering.chat_view import ChatView, LiveSink
from boba.aid.rendering.errors import show_error
from boba.aid.rendering.question import QuestionCards
from boba.aid.rendering.renderer import RenderSurface, TabSurfaces
from boba.aid.rendering.tool import ChatElements
from boba.cancellation import RunCancellation
from boba.identity.context import Scope
from boba.identity.errors import InternalServiceError
from boba.identity.locks import LiveLocks, LockKeeper, LockMode, LockPurpose
from boba.mcp_client.client import McpCaller, McpServers
from boba.messaging import MessageBus, NoticeLevel, PayloadStore, TurnOutcome
from boba.runtime.storage import StorageClient
from boba.transport.acp.protocol import ErrorDataKey

__all__ = [
    "AgentFeed",
    "AgentFeeds",
    "AskEvent",
    "BusParts",
    "QuestionAsk",
    "QuestionReply",
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


class AskEvent(StrEnum):
    """События chainlit вокруг вопроса пользователю: показ панели с ожиданием
    ответа, снятие панели и элемента вопроса во вкладке."""

    ASK = "ask"
    CLEAR = "clear_ask"
    REMOVE_ELEMENT = "remove_element"


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

    async def signal(self, event: AskEvent, payload: Mapping[str, Any]) -> None:
        """Событие вопроса вкладке."""
        # chainlit типизирует emit сессии без ожидания, хотя отдаёт корутину
        await cast("Awaitable[None]", self.session.emit(event.value, dict(payload)))

    async def asked(self, payload: Mapping[str, Any], timeout: int) -> object:
        """`ask` вкладке: ответ её колбэка; срок socket.io — timeout."""
        return await self.session.emit_call(AskEvent.ASK.value, dict(payload), timeout)


class TabSurface:
    """Контекст chainlit одной вкладки: воспроизведение истории рисуется
    только ей, остальные вкладки треда воспроизводят её сами."""

    def __init__(self, anchor: WebsocketSession) -> None:
        self._anchor = anchor
        self._emitter = TabEmitter(anchor)

    @property
    def emitter(self) -> TabEmitter:
        return self._emitter

    @property
    def session_id(self) -> str:
        return self._anchor.id

    @contextmanager
    def entered(self) -> Iterator[None]:
        token = context_var.set(ChainlitContext(self._anchor, self._emitter))
        try:
            yield
        finally:
            context_var.reset(token)


class QuestionReply(BaseModel):
    """Ответ панели вопроса из колбэка `ask`: действие и поля формы; флаг
    submitted chainlit добавляет сам."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    action: Literal["accept", "decline"]
    content: dict[str, str | int | float | bool | list[str]] = Field(
        default_factory=dict
    )

    def response(self) -> ElicitationReply:
        if self.action == "decline":
            return schema.DeclineElicitationResponse()

        return schema.AcceptElicitationResponse(content=self.content)


class QuestionAsk:
    """Висящий вопрос агента во вкладке: элемент Question и `ask` chainlit до
    ответа пользователя, конца срока или снятия.

    Вкладка, подключившаяся заново, получает панель снова (show_in): прежнее
    ожидание снимается, его вкладка панель убирает. Каждое ожидание
    кончается снятием панели и элемента и возвратом индикатора хода — `ask`
    chainlit его гасит. Создаёт приёмник AgentFeed на каждую форму агента.
    """

    GRACE_SEC: ClassVar[int] = 5
    """Запас срока ожидания socket.io поверх своего: срок ведёт сам вопрос."""

    def __init__(
        self,
        element: CustomElement,
        step: StepDict,
        step_id: str,
        tab: TabSurface,
        timeout_sec: float,
    ) -> None:
        self._element = element
        self._step = step
        self._step_id = step_id
        self._tab = tab
        self._timeout_sec = timeout_sec
        self._asking: asyncio.Future[QuestionReply | None] | None = None
        self._stopped = False

    async def reply(self) -> QuestionReply | None:
        """Ответ пользователя; None — срок вышел или вопрос снят.

        Ошибки:
        ValidationError — ответ вкладки не по форме ответа.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._timeout_sec
        try:
            while not self._stopped:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    return None

                asking = asyncio.ensure_future(self._asked(self._tab, remaining))
                self._asking = asking
                await asyncio.wait((asking,))
                if not asking.cancelled():
                    return asking.result()

            return None
        finally:
            if self._asking is not None:
                self._asking.cancel()

    @property
    def tab_id(self) -> str:
        """Сессия вкладки, которой показан вопрос."""
        return self._tab.session_id

    def show_in(self, tab: TabSurface) -> None:
        """Панель — во вкладку tab: текущее ожидание снимается и начинается
        заново там."""
        self._tab = tab
        if self._asking is not None:
            self._asking.cancel()

    def stop(self) -> None:
        """Вопрос снят без пользователя: ход останавливается."""
        self._stopped = True
        if self._asking is not None:
            self._asking.cancel()

    async def _asked(self, tab: TabSurface, remaining: float) -> QuestionReply | None:
        emitter = tab.emitter
        spec = AskElementSpec(
            type="element",
            step_id=self._step_id,
            element_id=self._element.id,
            timeout=math.ceil(remaining) + self.GRACE_SEC,
        )
        await emitter.send_element(self._element.to_dict())
        try:
            async with asyncio.timeout(remaining):
                raw = await emitter.asked(
                    {"msg": self._step, "spec": spec.to_dict()}, spec.timeout
                )
        except TimeoutError:
            return None
        finally:
            await emitter.signal(AskEvent.CLEAR, {})
            await emitter.signal(AskEvent.REMOVE_ELEMENT, {"id": self._element.id})
            await emitter.task_start()

        return QuestionReply.model_validate(raw)


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
    )
    """Уведомления сессии, которых в ленте нет: заголовок, план, команды,
    сжатие и состояние идущего хода."""

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
        question_timeout_sec: float,
    ) -> None:
        self._identity = identity
        self._options = options
        self._replay = replay
        self._tab = tab
        self._surface = surface
        self._parts = parts
        self._rows = rows
        self._question_timeout_sec = question_timeout_sec
        self._scope = Scope.chat(identity.thread_id)
        self._cards = QuestionCards()
        self._elements = ChatElements()
        self._turn: LiveTurn | None = None
        self._stopping: set[str] = set()
        self._ordered = asyncio.Lock()
        self._asking = asyncio.Lock()
        """Вопросы пользователю показываются по одному, в порядке прихода."""
        self._ask: QuestionAsk | None = None

    @property
    def thread_id(self) -> str:
        return self._identity.thread_id

    def stopping(self, session_id: str) -> None:
        self._stopping.add(session_id)
        if self._ask is not None:
            self._ask.stop()

    async def question_shown(
        self, anchor: WebsocketSession, live: Collection[str]
    ) -> bool:
        ask = self._ask
        if ask is None:
            return False

        shown_in = ask.tab_id
        if shown_in != anchor.id and shown_in in live:
            return False

        ask.show_in(TabSurface(anchor))
        logger.info(
            "thread %s: the waiting question is shown again in session %s",
            self.thread_id,
            anchor.id,
        )

        return True

    async def elicitation(
        self, request: schema.CreateFormSessionElicitationRequest
    ) -> ElicitationReply:
        """Форма агента — панелью во вкладке держателя хода; ответ панели —
        ответ агенту, срок и остановка хода — отмена. Вопрос вне хода не
        показывается. Форма встаёт в очередь уведомлений: ход, начало которого
        ещё публикуется, и вызов, задавший вопрос, видны раньше неё.

        Ошибки:
        RequestError — ответ вкладки не по форме ответа.
        """
        async with self._ordered:
            turn = self._turn

        if turn is None:
            logger.warning(
                "thread %s: the agent asked the user outside a turn of session %s; "
                "the question is cancelled",
                self.thread_id,
                request.session_id,
            )
            return schema.CancelElicitationResponse()

        card = self._cards.pending(request)
        # панель живёт только у поля ввода: шаг ask не совпадает ни с одним
        # сообщением ленты, иначе элемент встал бы в него второй строкой
        step_id = str(uuid4())
        element = self._elements.question(card.props())
        element.id = str(uuid4())
        element.thread_id = self.thread_id
        element.for_id = step_id
        step = StepDict(id=step_id, parentId=None, threadId=self.thread_id)
        async with self._asking:
            if request.session_id in self._stopping:
                return schema.CancelElicitationResponse()

            ask = QuestionAsk(
                element, step, step_id, self._tab, self._question_timeout_sec
            )
            self._ask = ask
            try:
                reply = await ask.reply()
            except ValidationError as exc:
                logger.error(
                    "thread %s: the answer of the question of call %s is off the "
                    "form: %s",
                    self.thread_id,
                    request.tool_call_id,
                    exc,
                )
                details = (
                    f"the tab answered the question of call {request.tool_call_id} "
                    f"off the answer form: {exc}"
                )
                raise RequestError.internal_error(
                    {ErrorDataKey.DETAILS.value: details}
                ) from exc
            finally:
                self._ask = None

        if reply is None:
            return schema.CancelElicitationResponse()

        return reply.response()

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

        # конец живого хода публикует turn_finished, после всех его уведомлений
        if isinstance(update, schema.IdleSessionStateUpdate):
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
        """Конец хода в шину: idle — уведомлением хода, его ленты рисуют
        пометку остановки или сбоя тем же переводчиком, что и историю; затем
        итог хода с исходом по тому же idle; замок области отпускается."""
        self._stopping.discard(session_id)
        async with self._ordered:
            await self._finish(idle)

    async def _finish(self, idle: schema.IdleSessionStateUpdate) -> None:
        turn = self._turn
        if turn is None:
            return

        ending = self._replay.ending(idle)
        try:
            await turn.feed.agent_update(idle)
            await turn.feed.finished(ending.outcome, ending.note)
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
    инструментов серверов профиля (вид аргументов, журнал) переводчик берёт
    у портов клиента MCP чата от имени владельца треда (ServerCards); сервер,
    который не ответил, — его вызовы рисуются без карточек, о самом сервере
    пользователю сообщает агент. Срок ответа на вопрос агента — из [agent].
    Создаётся провайдером процесса.
    """

    def __init__(  # noqa: PLR0913 — фабрика собирается всеми входами процесса
        self,
        parts: BusParts,
        journals: RemoteJournals,
        servers: McpServers,
        rows: FeedRows,
        surfaces: TabSurfaces,
        storage: StorageClient,
        texts: UiText,
        question_timeout_sec: float,
    ) -> None:
        self._parts = parts
        self._texts = texts
        self._journals = journals
        self._servers = servers
        self._rows = rows
        self._surfaces = surfaces
        self._storage = storage
        self._question_timeout_sec = question_timeout_sec

    async def create(
        self, identity: ThreadIdentity, options: SessionOptions
    ) -> AgentFeed:
        """Ошибки:
        InternalServiceError — у текущей сессии chainlit нет сокета.
        """
        caller = McpCaller(login=identity.login, roles=identity.roles)
        media = ToolMedia(ServerCards(self._servers, caller))
        anchor = self._surfaces.anchor()
        view = ChatView(
            identity.thread_id,
            LiveSink(),
            str(identity.user_id),
            self._storage,
            self._texts,
            user_name=identity.login,
        )
        replay = UpdateView(view, identity.thread_id, self._journals, media)

        return AgentFeed(
            identity,
            options,
            replay,
            TabSurface(anchor),
            self._surfaces.thread(anchor, identity.thread_id),
            self._parts,
            self._rows,
            self._question_timeout_sec,
        )
