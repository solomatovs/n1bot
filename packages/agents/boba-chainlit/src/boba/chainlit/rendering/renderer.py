"""Отрисовка ленты chainlit по сообщениям шины: ChatRenderer подписан на область
треда, владеет ChatView и превращает каждое сообщение в шаг ленты.

Ошибки:
Сбой отрисовки одного сообщения показывается в чат и журналируется; когда показать
    некуда (поверхности нет), ошибка поднимается шине.
"""

from __future__ import annotations

import logging
from abc import abstractmethod
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, ClassVar, Protocol

from boba.canvas.canvas import CanvasSignal
from boba.chainlit.domain.fields import StepField, ThreadField
from boba.chainlit.rendering.chat_view import ChatView, StepText
from boba.chainlit.rendering.errors import show_error
from boba.identity.context import Scope
from boba.identity.errors import FailureReport
from boba.messaging import (
    AnswerClosed,
    AnswerInterrupted,
    AnswerToken,
    CanvasChanged,
    Envelope,
    MessageBus,
    MessageKind,
    ModelAnswered,
    Notice,
    NoticeLevel,
    PayloadStore,
    SignInRefreshRequested,
    StageEnded,
    StageQueries,
    StageStarted,
    ThinkingClosed,
    ThinkingComplete,
    ThinkingToken,
    ToolFailed,
    ToolFinished,
    ToolStarted,
    ToolStopped,
    TurnFinished,
    TurnStarted,
    Unsubscribe,
)
from chainlit.context import ChainlitContext, context_var
from chainlit.step import Step, StepDict
from chainlit.types import ThreadDict

__all__ = ["ChatRenderer", "ChatRenderers", "NoSurface", "RenderSurface"]

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
    async def task_start(self) -> None:
        """Включает индикатор хода во всех вкладках треда."""

    @abstractmethod
    async def task_end(self) -> None:
        """Гасит индикатор хода во всех вкладках треда."""


class NoSurface(RenderSurface):
    """Поверхность без вывода: лента пишется только в sink, как при сборке истории и в
    тестах.
    """

    def context(self) -> ChainlitContext | None:
        return None

    async def window_message(self, payload: Mapping[str, Any]) -> None:
        return None

    async def task_start(self) -> None:
        return None

    async def task_end(self) -> None:
        return None


class SignalType:
    """Метки window_message: по ним скрипты страницы отличают свой сигнал от чужого."""

    KERBEROS_REFRESH: ClassVar[str] = "boba:kerberos-refresh"


class ChatRenderer:
    """Получатель сообщений одного треда: превращает конверты шины в шаги ленты
    chainlit и держит карту шагов инструментов по call_id.
    """

    def __init__(
        self,
        thread_id: str,
        view: ChatView,
        payloads: PayloadStore,
        surface: RenderSurface,
    ) -> None:
        self._thread_id = thread_id
        self._view = view
        self._payloads = payloads
        self._surface = surface
        self._scope = Scope.chat(thread_id)
        self._tool_steps: dict[str, Step] = {}
        self._turn_key: str | None = None
        self._handlers = self._handler_table()

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

        await handler(envelope.message)

    def _handler_table(self) -> Mapping[MessageKind, Handler]:
        return {
            MessageKind.TURN_STARTED: self._on_turn_started,
            MessageKind.MODEL_ANSWERED: self._on_model_answered,
            MessageKind.ANSWER_TOKEN: self._on_answer_token,
            MessageKind.ANSWER_CLOSED: self._on_answer_closed,
            MessageKind.ANSWER_INTERRUPTED: self._on_answer_interrupted,
            MessageKind.THINKING_TOKEN: self._on_thinking_token,
            MessageKind.THINKING_COMPLETE: self._on_thinking_complete,
            MessageKind.THINKING_CLOSED: self._on_thinking_closed,
            MessageKind.STAGE_STARTED: self._on_stage_started,
            MessageKind.STAGE_QUERIES: self._on_stage_queries,
            MessageKind.STAGE_ENDED: self._on_stage_ended,
            MessageKind.TOOL_STARTED: self._on_tool_started,
            MessageKind.TOOL_FINISHED: self._on_tool_finished,
            MessageKind.TOOL_FAILED: self._on_tool_failed,
            MessageKind.TOOL_STOPPED: self._on_tool_stopped,
            MessageKind.TURN_FINISHED: self._on_turn_finished,
            MessageKind.NOTICE: self._on_notice,
            MessageKind.CANVAS_CHANGED: self._on_canvas_changed,
            MessageKind.SIGNIN_REFRESH_REQUESTED: self._on_signin_refresh,
        }

    def begin_turn(self, key: str) -> None:
        """Открывает ход в ленте: ключ хода адресует контейнер, ответ и шаг ошибки."""
        self._turn_key = key
        self._view.begin_turn(key)

    async def _on_turn_started(self, message: TurnStarted) -> None:
        self.begin_turn(message.key)
        await self._surface.task_start()
        await self._view.await_model()

    async def _on_model_answered(self, message: ModelAnswered) -> None:
        await self._view.model_answered()

    async def _on_answer_token(self, message: AnswerToken) -> None:
        await self._view.stream_answer(message.token, message.key)

    async def _on_answer_closed(self, message: AnswerClosed) -> None:
        await self._view.close_answer(message.key)

    async def _on_answer_interrupted(self, message: AnswerInterrupted) -> None:
        await self._interrupt_answer(message.key, message.note)

    async def _on_thinking_token(self, message: ThinkingToken) -> None:
        await self._view.stream_thinking(message.token, message.key)

    async def _on_thinking_complete(self, message: ThinkingComplete) -> None:
        text = await self._payloads.get(message.text)
        await self._view.thinking(str(text), message.key)

    async def _on_thinking_closed(self, message: ThinkingClosed) -> None:
        await self._view.close_thinking()

    async def _on_stage_started(self, message: StageStarted) -> None:
        await self._view.begin_stage(message.name, message.phase)

    async def _on_stage_queries(self, message: StageQueries) -> None:
        await self._view.stage_queries(message.queries)

    async def _on_stage_ended(self, message: StageEnded) -> None:
        await self._view.end_stage(message.queries, message.elapsed_ms)

    async def _on_tool_started(self, message: ToolStarted) -> None:
        args = await self._payloads.get(message.args)
        if not isinstance(args, Mapping):
            args = {}

        step = await self._view.tool_started(message.name, args, message.call_id)
        self._tool_steps[message.call_id] = step

    async def _on_tool_finished(self, message: ToolFinished) -> None:
        step = self._tool_steps.pop(message.call_id, None)
        if step is None:
            return

        result = await self._payloads.get(message.result)
        await self._view.tool_finished(step, result, message.call_id)

    async def _on_tool_failed(self, message: ToolFailed) -> None:
        step = self._tool_steps.pop(message.call_id, None)
        if step is None:
            return

        await self._view.tool_failed(step, message.error)

    async def _on_tool_stopped(self, message: ToolStopped) -> None:
        step = self._tool_steps.pop(message.call_id, None)
        if step is None:
            return

        await self._view.tool_stopped(step, message.note)

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

    async def _on_signin_refresh(self, message: SignInRefreshRequested) -> None:
        await self._surface.window_message({"type": SignalType.KERBEROS_REFRESH})

    async def _interrupt_answer(self, key: str, note: str) -> None:
        """Дописывает к накопленному ответу курсивную пометку об остановке; без ответа
        пометка становится ответом.
        """
        marker = f"_{note}_"
        answer = self._view.answer_message
        if answer is None:
            await self._view.answer(marker, key)
            return

        partial = answer.content
        if not partial:
            await self._view.rewrite_answer(marker, key)
            return

        await self._view.rewrite_answer(f"{partial}\n\n{marker}", key)

    async def _finish_turn(self) -> None:
        for step in list(self._tool_steps.values()):
            await self._view.tool_stopped(step, StepText.FINISHED.value)

        self._tool_steps.clear()
        self._turn_key = None
        await self._view.finish_turn()
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

    def resume_steps(self) -> list[StepDict]:
        """Возвращает открытые шаги хода для вкладки, подключившейся посреди хода: в
        истории их ещё нет.
        """
        steps: list[StepDict] = []
        if container := self._view.container_step:
            steps.append(container.to_dict())

        if thinking := self._view.thinking_step:
            steps.append(thinking.to_dict())

        for step in self._tool_steps.values():
            steps.append(step.to_dict())

        if answer := self._view.answer_message:
            steps.append(answer.to_dict())

        if pulse := self._view.pulse_step:
            steps.append(pulse.to_dict())

        return steps

    def resume_into(self, thread_dict: ThreadDict) -> None:
        """Подкладывает открытые шаги хода в ленту треда до её отправки клиенту."""
        live = self.resume_steps()

        steps = list(thread_dict.get(ThreadField.STEPS) or [])
        positions: dict[str, int] = {}
        for index, step in enumerate(steps):
            positions[step.get(StepField.ID, "")] = index

        for step in live:
            index = positions.get(step.get(StepField.ID, ""))
            if index is None:
                steps.append(step)
            else:
                steps[index] = step

        thread_dict[ThreadField.STEPS] = steps

        names: list[str] = []
        for step in live:
            names.append(str(step.get(StepField.NAME, "")))

        logger.info(
            "resume thread %s: %d live steps merged (%s)",
            self._thread_id,
            len(live),
            ", ".join(names),
        )


class ChatRenderers:
    """Реестр рендереров тредов процесса и их подписок на шину."""

    _RENDERERS: ClassVar[dict[str, ChatRenderer]] = {}
    _LEAVES: ClassVar[dict[str, Unsubscribe]] = {}

    @classmethod
    def ensure(
        cls,
        thread_id: str,
        bus: MessageBus,
        view: ChatView,
        payloads: PayloadStore,
        surface: RenderSurface,
    ) -> ChatRenderer:
        """Возвращает рендерер треда, создавая и подписывая его на область при первом
        обращении.
        """
        renderer = cls._RENDERERS.get(thread_id)
        if renderer is not None:
            return renderer

        renderer = ChatRenderer(thread_id, view, payloads, surface)
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
