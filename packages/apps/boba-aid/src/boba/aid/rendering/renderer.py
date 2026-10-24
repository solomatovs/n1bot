"""Отрисовка ленты chainlit по сообщениям шины: ChatRenderer подписан на область
треда, владеет переводчиком уведомлений агента (UpdateView над ChatView) и
превращает каждое сообщение в шаг ленты: вопрос и итог хода — по TurnStarted и
TurnFinished держателя, сами шаги — по уведомлениям `session/update`, которые
держатель пересылает как есть (AgentUpdate), кнопка журнала идущего вызова —
по JournalOpened ретранслятора. Срез истории правкой вопроса (ThreadRewound)
рендерер передаёт порту ThreadRewinds: вкладки треда перечитывают ленту из
истории своих агентов.

Ошибки:
Сбой отрисовки одного сообщения показывается в чат и журналируется; когда показать
    некуда (поверхности нет), ошибка поднимается шине.
"""

from __future__ import annotations

import asyncio
import logging
from abc import abstractmethod
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any, ClassVar, Protocol

from chainlit.context import ChainlitContext, context_var
from chainlit.session import WebsocketSession
from chainlit.step import StepDict
from pydantic import BaseModel, ConfigDict

from boba.aid.chat.feed import QuestionBody
from boba.aid.rendering.acp_view import SessionUpdateCodec, UpdateView
from boba.aid.rendering.chat_view import ChatView
from boba.aid.rendering.errors import show_error
from boba.canvas.canvas import CanvasSignal
from boba.identity.context import Scope
from boba.identity.errors import FailureReport
from boba.messaging import (
    AgentUpdate,
    AnswerInterrupted,
    CanvasChanged,
    Envelope,
    FeedbackChanged,
    JournalOpened,
    MessageBus,
    MessageKind,
    Notice,
    NoticeLevel,
    PayloadStore,
    ThreadRewound,
    TurnFinished,
    TurnStarted,
    Unsubscribe,
)

__all__ = [
    "CatchUp",
    "ChatRenderer",
    "ChatRenderers",
    "NoSurface",
    "RenderSurface",
    "TabSurfaces",
    "ThreadRewinds",
]

logger = logging.getLogger(__name__)

Handler = Callable[[Any], Awaitable[None]]


class RenderSurface(Protocol):
    """Порт вывода рендерера: контекст chainlit для ленты, сигналы странице и
    индикатор хода.
    """

    @abstractmethod
    def context(self) -> ChainlitContext | None:
        """Возвращает контекст chainlit, в котором рисуется лента; None означает, что
        рисовать некуда.
        """

    @abstractmethod
    async def window_message(self, payload: Mapping[str, Any]) -> None:
        """Шлёт странице сигнал (window.postMessage) во все живые вкладки треда."""

    @abstractmethod
    async def resume_thread(self) -> None:
        """Перечитывает тред из истории и отдаёт ленту всем живым вкладкам треда."""

    @abstractmethod
    async def refresh_step(self, step_id: str) -> None:
        """Перечитывает шаг из истории и обновляет его во всех вкладках треда."""

    @abstractmethod
    async def task_start(self) -> None:
        """Включает индикатор хода во всех вкладках треда."""

    @abstractmethod
    async def task_end(self) -> None:
        """Гасит индикатор хода во всех вкладках треда."""


class TabSurfaces(Protocol):
    """Откуда у приёмника вкладка и поверхность рассылки треда: сокет текущего
    обработчика и контекст с эмиттером по всем вкладкам треда, а для показа
    вне обработчика (ответ tools/call в ретрансляторе) — поверхность любой
    живой вкладки треда. Реализует ThreadSurfaces (infra.thread_room)."""

    @abstractmethod
    def anchor(self) -> WebsocketSession:
        """Сокет текущего обработчика chainlit.

        Ошибки:
        InternalServiceError — у текущей сессии нет сокета.
        """

    @abstractmethod
    def thread(self, anchor: WebsocketSession, thread_id: str) -> RenderSurface:
        """Поверхность рассылки по всем вкладкам треда с якорной сессией."""

    @abstractmethod
    def live(self, thread_id: str) -> RenderSurface:
        """Поверхность рассылки треда с якорем в любой его живой вкладке;
        без живых вкладок на этом инстансе — NoSurface."""


class ThreadRewinds(Protocol):
    """Кому рендерер треда сообщает, что история треда заменена веткой после
    правки вопроса. Реализует ThreadAgentsIndex: рендерер агентов не знает,
    агент на шину не подписан."""

    @abstractmethod
    async def rewound(self, thread_id: str, session_id: str) -> None:
        """Вкладки треда, чья сессия не ветка session_id, перечитывают ленту."""


class NoSurface(RenderSurface):
    """Поверхность без вывода: лента пишется только в sink, как при сборке истории и
    в тестах.
    """

    def context(self) -> ChainlitContext | None:
        return None

    async def window_message(self, payload: Mapping[str, Any]) -> None:
        return None

    async def resume_thread(self) -> None:
        return None

    async def refresh_step(self, step_id: str) -> None:
        return None

    async def task_start(self) -> None:
        return None

    async def task_end(self) -> None:
        return None


class SignalType:
    """Метки window_message: по ним скрипты страницы отличают свой сигнал от чужого."""

    SIGNIN_REFRESH: ClassVar[str] = "boba:signin-refresh"
    COMPOSER_RESTORE: ClassVar[str] = "boba:composer-restore"
    """Тексты вопросов, снятых остановкой хода, — обратно в поле ввода."""
    THREAD_ACTIVITY: ClassVar[str] = "boba:thread-activity"
    """Новое время активности треда из базы — панели тредов."""


class CatchUp(BaseModel):
    """Итог догона области: идёт ли ход, и если последний ход закрыл сторож за
    умершего держателя — причина в interrupted (иначе пусто).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    alive: bool
    interrupted: str


class ChatRenderer:
    """Получает сообщения одного треда из шины и превращает их в шаги ленты
    chainlit; уведомления агента переводит в шаги UpdateView — тот же
    переводчик, что рисует историю при `session/resume`. Сообщения рисуются
    по одному в порядке прихода, хотя публикуют их разные задачи (агент,
    ретранслятор): шаг вызова, который ждёт карточку инструмента у сервера,
    достраивается раньше, чем до него дойдёт журнал этого вызова.
    """

    def __init__(
        self,
        thread_id: str,
        updates: UpdateView,
        payloads: PayloadStore,
        surface: RenderSurface,
        rewinds: ThreadRewinds,
    ) -> None:
        self._thread_id = thread_id
        self._updates = updates
        self._view = updates.view
        self._payloads = payloads
        self._surface = surface
        self._rewinds = rewinds
        self._scope = Scope.chat(thread_id)
        self._codec = SessionUpdateCodec()
        self._turn_key: str | None = None
        self._handlers = self._handler_table()
        self._order = asyncio.Lock()

    @property
    def view(self) -> ChatView:
        return self._view

    @property
    def turn_alive(self) -> bool:
        """Ход начат сообщением TurnStarted и ещё не закончен TurnFinished."""
        return self._turn_key is not None

    async def apply(self, envelope: Envelope) -> None:
        context = self._surface.context()
        if context is None:
            await self._render(envelope)
            return

        token = context_var.set(context)
        try:
            await self._shown(envelope)
        finally:
            context_var.reset(token)

    async def _shown(self, envelope: Envelope) -> None:
        """Граница отрисовки: сбой одного сообщения показывается в чат и в журнал одним
        разбором FailureReport.
        """
        try:
            await self._render(envelope)
        except Exception as exc:
            report = FailureReport.of(exc)
            logger.exception(
                "thread %s: rendering of %s (seq %d) failed: %s",
                self._thread_id,
                envelope.message.kind,
                envelope.seq,
                report.log,
            )
            if not report.view:
                raise

            await show_error(f"Failed to render {envelope.message.kind}: {report.view}")

    async def _render(self, envelope: Envelope) -> None:
        handler = self._handlers.get(envelope.message.kind)
        if handler is None:
            return

        async with self._order:
            await handler(envelope.message)

    def _handler_table(self) -> Mapping[MessageKind, Handler]:
        return {
            MessageKind.TURN_STARTED: self._on_turn_started,
            MessageKind.AGENT_UPDATE: self._on_agent_update,
            MessageKind.ANSWER_INTERRUPTED: self._on_answer_interrupted,
            MessageKind.TURN_FINISHED: self._on_turn_finished,
            MessageKind.NOTICE: self._on_notice,
            MessageKind.CANVAS_CHANGED: self._on_canvas_changed,
            MessageKind.JOURNAL_OPENED: self._on_journal_opened,
            MessageKind.THREAD_REWOUND: self._on_thread_rewound,
            MessageKind.FEEDBACK_CHANGED: self._on_feedback_changed,
        }

    def begin_turn(self, key: str) -> None:
        """Открывает ход в ленте: ключ хода адресует контейнер, ответ и шаг ошибки."""
        self._turn_key = key
        self._updates.begin_turn(key)

    async def _on_turn_started(self, message: TurnStarted) -> None:
        logger.info(
            "thread %s: turn %s is rendered here", self._thread_id, message.turn_id
        )
        self.begin_turn(message.key)
        # вопрос и его вложения рисуются всем вкладкам треда, и отправившей тоже
        question = QuestionBody.model_validate(
            await self._payloads.get(message.question)
        )
        await self._updates.question(
            question.text, message.key, question.author, question.links
        )

        await self._surface.task_start()
        await self._view.await_model()

    async def _on_agent_update(self, message: AgentUpdate) -> None:
        body: object = message.inline
        if message.stored is not None:
            body = await self._payloads.get(message.stored)

        await self._updates.apply(self._codec.decode(body))

    async def _on_answer_interrupted(self, message: AnswerInterrupted) -> None:
        await self._interrupt_answer(message.key, message.note)

    async def _on_turn_finished(self, message: TurnFinished) -> None:
        await self._finish_turn()

    async def _on_notice(self, message: Notice) -> None:
        await self._notice(message.level, message.text)

    async def _on_canvas_changed(self, message: CanvasChanged) -> None:
        signal = CanvasSignal(
            path=message.path,
            nonce=message.nonce,
            revision=message.revision,
            size=message.size,
            closed=message.closed,
            note=message.note,
        )
        await self._surface.window_message(signal.payload())

    async def _on_journal_opened(self, message: JournalOpened) -> None:
        await self._updates.journal_opened(message)

    async def _on_thread_rewound(self, message: ThreadRewound) -> None:
        await self._rewinds.rewound(self._thread_id, message.session_id)

    async def _on_feedback_changed(self, message: FeedbackChanged) -> None:
        await self._surface.refresh_step(message.step_id)

    async def _interrupt_answer(self, key: str, note: str) -> None:
        await self._view.interrupt_answer(note, key)

    async def _finish_turn(self) -> None:
        self._turn_key = None
        await self._updates.finish_turn()
        await self._surface.task_end()
        await self._payloads.purge(self._scope)

    async def _notice(self, level: NoticeLevel, text: str) -> None:
        if self._turn_key is not None:
            await self._view.error(text, self._turn_key)
            return

        if self._surface.context() is None:
            msg = f"thread {self._thread_id}: notice without a surface: {text}"
            raise RuntimeError(msg)

        await show_error(text, author=level.value.capitalize())

    async def catch_up(self, bus: MessageBus) -> CatchUp:
        """Догоняет область по сохранённым сообщениям шины: применяет всё от
        последнего TurnStarted, если ход не закончен, иначе сообщает, чем он
        закончился.
        """
        stored = await bus.replay(self._scope, 0)

        start = -1
        for index, envelope in enumerate(stored):
            if envelope.message.kind is MessageKind.TURN_STARTED:
                start = index

        if start < 0:
            return CatchUp(alive=False, interrupted="")

        finished = self._finished_after(stored[start:])
        if finished is not None:
            return CatchUp(alive=False, interrupted=self._interruption(finished))

        for envelope in stored[start:]:
            await self.apply(envelope)

        return CatchUp(alive=self.turn_alive, interrupted="")

    @staticmethod
    def _finished_after(live: Sequence[Envelope]) -> TurnFinished | None:
        for envelope in live:
            message = envelope.message
            if isinstance(message, TurnFinished):
                return message

        return None

    @staticmethod
    def _interruption(finished: TurnFinished) -> str:
        """Возвращает причину остановки, если ход закрыл сторож за умершего
        держателя; иначе пустую строку.
        """
        if finished.reason != TurnFinished.HOLDER_GONE:
            return ""

        return finished.reason

    def resume_steps(self) -> list[StepDict]:
        """Возвращает открытые шаги хода для вкладки, подключившейся посреди хода,
        потому что в истории их ещё нет.
        """
        steps: list[StepDict] = []
        if container := self._view.container_step:
            steps.append(container.to_dict())

        if thinking := self._view.thinking_step:
            steps.append(thinking.to_dict())

        for step in self._updates.open_steps():
            steps.append(step.to_dict())

        if answer := self._view.answer_message:
            steps.append(answer.to_dict())

        if pulse := self._view.pulse_step:
            steps.append(pulse.to_dict())

        return steps


class ChatRenderers:
    """Реестр рендереров тредов процесса и их подписок на шину."""

    _RENDERERS: ClassVar[dict[str, ChatRenderer]] = {}
    _LEAVES: ClassVar[dict[str, Unsubscribe]] = {}

    @classmethod
    def ensure(  # noqa: PLR0913 — рендерер треда собирается всеми входами
        cls,
        thread_id: str,
        bus: MessageBus,
        updates: UpdateView,
        payloads: PayloadStore,
        surface: RenderSurface,
        rewinds: ThreadRewinds,
    ) -> ChatRenderer:
        """Возвращает рендерер треда, создавая и подписывая его на область при первом
        обращении.
        """
        renderer = cls._RENDERERS.get(thread_id)
        if renderer is not None:
            return renderer

        renderer = ChatRenderer(thread_id, updates, payloads, surface, rewinds)
        cls._RENDERERS[thread_id] = renderer
        cls._LEAVES[thread_id] = bus.subscribe(Scope.chat(thread_id), renderer.apply)
        return renderer

    @classmethod
    def get(cls, thread_id: str) -> ChatRenderer | None:
        return cls._RENDERERS.get(thread_id)

    @classmethod
    def drop(cls, thread_id: str) -> None:
        cls._RENDERERS.pop(thread_id, None)
        leave = cls._LEAVES.pop(thread_id, None)
        if leave is not None:
            leave()

    @classmethod
    def release(cls, thread_id: str, in_use: bool) -> None:
        """Снимает рендерер треда, когда ход закончен и вкладок у треда не осталось."""
        renderer = cls._RENDERERS.get(thread_id)
        if renderer is None:
            return

        if in_use:
            return

        if renderer.turn_alive:
            return

        cls.drop(thread_id)

    @classmethod
    def reset(cls) -> None:
        """Снимает все рендереры; нужен тестам, чтобы стенды не делили состояние."""
        for thread_id in list(cls._RENDERERS):
            cls.drop(thread_id)
