"""Ход агента в чате: прогон стрима, остановка, рассылка во все сокеты треда.

Ход живёт дольше сокета: вкладку обновили, а стрим продолжается. ThreadEmitter
решает адресатов в момент эмиссии — каждое событие уходит всем живым сессиям
треда, новая вкладка подхватывает стрим сразу после подключения.

Ход обрывается только по кнопке Stop: отмену держит TurnRegistry по thread_id.
Разрыв связи ход не трогает — ответ пользователь увидит в истории.

Ошибки: наружу выходит только asyncio.CancelledError — её ждёт chainlit как
признак снятой задачи; прикладные сбои превращаются в сообщение об ошибке
в ленте и в истории.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any, ClassVar, cast

from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage, HumanMessage
from langchain_core.runnables import RunnableConfig
from langgraph.graph.state import CompiledStateGraph

import chainlit as cl
from boba.cancellation import StopReason, ToolStopped, TurnRegistry
from boba.chainlit.chat.agent_tracer import AgentTracer
from boba.chainlit.chat.data.object_key import ObjectKey
from boba.chainlit.infra.session import current_user_id
from boba.chainlit.rendering.chat_view import ChatView, StepText
from boba.sandbox import WORKSPACE_MOUNT
from chainlit.context import ChainlitContext, context_var, get_context
from chainlit.emitter import ChainlitEmitter
from chainlit.server import sio
from chainlit.session import WebsocketSession, ws_sessions_id
from chainlit.step import StepDict

__all__ = ["ChatTurn", "StickyLoadingEmitter", "ThreadEmitter", "ThreadRoom"]

logger = logging.getLogger(__name__)


class StickyLoadingEmitter(ChainlitEmitter):
    """Эмиттер resume-хендлера: не даёт chainlit погасить индикатор хода.

    wrap_user_function(with_task=True) шлёт task_end сразу после хендлера;
    при живом ходе loading обязан пережить resume, поэтому task_end глушится.
    """

    async def task_end(self) -> None:
        return None


class ThreadEmitter(ChainlitEmitter):
    """Эмиттер треда: события уходят каждому живому сокету, а не одной сессии."""

    def __init__(self, session: WebsocketSession, thread_id: str) -> None:
        super().__init__(session)
        self._thread_id = thread_id

    @property
    def emit(self) -> Callable[[str, Any], Awaitable[None]]:
        async def emit_to_thread(event: str, data: Any) -> None:
            for session in ThreadRoom.sessions(self._thread_id):
                # одна битая сессия не должна ронять весь ход
                try:
                    await cast("Awaitable[None]", session.emit(event, data))
                except Exception:
                    logger.warning(
                        "emit %s failed for session %s of thread %s",
                        event,
                        session.id,
                        self._thread_id,
                        exc_info=True,
                    )

        return emit_to_thread

    @property
    def emit_call(self) -> Any:
        sessions = ThreadRoom.sessions(self._thread_id)
        if self.session in sessions:
            return self.session.emit_call
        if sessions:
            return sessions[0].emit_call
        return self.session.emit_call


class ThreadRoom:
    """Живые сессии треда и переключение контекста хода на рассылку."""

    @staticmethod
    def sessions(thread_id: str) -> list[WebsocketSession]:
        """Сессии треда с живым сокетом; умершие ждут таймаута chainlit."""
        sessions: list[WebsocketSession] = []
        for session in list(ws_sessions_id.values()):
            if session.thread_id != thread_id:
                continue
            if not sio.manager.is_connected(session.socket_id, "/"):
                continue
            sessions.append(session)
        return sessions

    @staticmethod
    def activate(thread_id: str) -> None:
        """Подменяет контекст текущей задачи: эмиссии хода видят все вкладки."""
        current = get_context()
        session = cast("WebsocketSession", current.session)
        emitter = ThreadEmitter(session, thread_id)
        context_var.set(ChainlitContext(session, emitter))
        logger.info("broadcast on thread %s: session=%s", thread_id, session.id)

    @staticmethod
    def keep_loading() -> None:
        """Глушит task_end обёртки chainlit вокруг текущего хендлера."""
        current = get_context()
        session = cast("WebsocketSession", current.session)
        context_var.set(ChainlitContext(session, StickyLoadingEmitter(session)))


class ChatTurn:
    """Один ход: стрим ответа под отменой, зарегистрированной на thread_id."""

    _REPORTS: ClassVar[set[asyncio.Future[None]]] = set()
    """Живые отчёты об остановке: без ссылки задачу заберёт сборщик мусора."""

    _ACTIVE: ClassVar[dict[str, ChatTurn]] = {}
    """Идущие ходы по thread_id: переподключившаяся вкладка находит свой ход."""

    def __init__(
        self,
        graph: CompiledStateGraph,
        thread_id: str,
        view: ChatView,
        tracer: AgentTracer,
        key: str,
    ) -> None:
        self._graph = graph
        self._thread_id = thread_id
        self._view = view
        self._tracer = tracer
        self._key = key
        self._outcome = "ok"

    @classmethod
    def stop(cls, thread_id: str) -> bool:
        """Кнопка Stop: обрывает ход немедленно; False — останавливать нечего."""
        return TurnRegistry.instance().stop(thread_id, StopReason.USER_STOP)

    @classmethod
    def active(cls, thread_id: str) -> ChatTurn | None:
        """Живой ход треда; None — тред ничем не занят."""
        return cls._ACTIVE.get(thread_id)

    @staticmethod
    def human_message(msg: cl.Message) -> HumanMessage:
        """Сообщение пользователя; пути вложений — как их видит песочница."""
        attachments: list[dict[str, str]] = []
        for element in msg.elements or []:
            key = ObjectKey.build(
                current_user_id(), element.thread_id, element.name, element.id
            )
            attachments.append(
                {
                    "name": element.name or element.id,
                    "path": f"{WORKSPACE_MOUNT}/{key.in_thread()}",
                }
            )
        extra = {"attachments": attachments} if attachments else {}
        return HumanMessage(content=msg.content, id=msg.id, additional_kwargs=extra)

    def resume_steps(self) -> list[StepDict]:
        """Незавершённые шаги хода: в истории их ещё нет, вкладке нужны сразу."""
        steps: list[StepDict] = []
        if container := self._view.container_step:
            steps.append(container.to_dict())
        for step in self._tracer.pending_tool_steps:
            steps.append(step.to_dict())
        if answer := self._view.answer_message:
            steps.append(answer.to_dict())
        return steps

    async def run(
        self, stream: AsyncIterator[tuple[BaseMessage, dict[str, Any]]]
    ) -> None:
        """Гоняет стрим до конца либо до остановки; отменённый ход не молчит."""
        self._ACTIVE[self._thread_id] = self
        started = time.monotonic()
        logger.info("turn start: thread=%s key=%s", self._thread_id, self._key)
        try:
            await self._run(stream)
        finally:
            if self._ACTIVE.get(self._thread_id) is self:
                del self._ACTIVE[self._thread_id]
            elapsed_ms = int((time.monotonic() - started) * 1000)
            logger.info(
                "turn finished: thread=%s outcome=%s in %dms",
                self._thread_id,
                self._outcome,
                elapsed_ms,
            )
            await self._finish_ui()

    async def _run(
        self, stream: AsyncIterator[tuple[BaseMessage, dict[str, Any]]]
    ) -> None:
        with TurnRegistry.instance().open(self._thread_id) as cancellation:
            await cl.context.emitter.task_start()
            try:
                async for chunk, _metadata in stream:
                    cancellation.raise_if_cancelled()
                    await self._on_chunk(chunk)
            except asyncio.CancelledError:
                # задачу сняли снаружи; после кнопки Stop причина уже своя
                cancellation.cancel(StopReason.ABORTED)
                self._outcome = self._reason_label(cancellation.reason)
                await self._report_stop(cancellation.reason)
                raise
            except ToolStopped:
                self._outcome = self._reason_label(cancellation.reason)
                await self._report_stop(cancellation.reason)
                return
            except Exception as e:
                logger.exception("agent run failed")
                self._outcome = StopReason.FAILED.value
                cancellation.cancel(StopReason.FAILED)
                await self._report_failure(e)
                return

        await self._view.close_answer(self._key)

    @staticmethod
    def _reason_label(reason: StopReason | None) -> str:
        if reason is None:
            return "stopped"
        return reason.value

    async def _finish_ui(self) -> None:
        """Гасит loading во всех вкладках треда: chainlit шлёт task_end только
        сессии, начавшей ход, а после F5 она мертва."""
        end = asyncio.ensure_future(cl.context.emitter.task_end())
        self._REPORTS.add(end)
        end.add_done_callback(self._REPORTS.discard)
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.shield(end)

    async def _report_stop(self, reason: StopReason | None) -> None:
        """Отчёт идёт своей задачей: повторная отмена хода не должна его съесть.

        Кнопку Stop chainlit обрабатывает отменой задачи хода, а прерыватель
        отменяет её второй раз — эта отмена приходит уже во время отрисовки.
        """
        report = asyncio.ensure_future(self._stopped(reason))
        self._REPORTS.add(report)
        report.add_done_callback(self._REPORTS.discard)
        await asyncio.shield(report)

    async def _stopped(self, reason: StopReason | None) -> None:
        """Фиксирует остановку: закрывает шаги и кладёт прерванный ответ в историю."""
        note_text = StepText.for_stop(reason)
        answer = self._view.answer_message
        partial = (answer.content if answer else "") or ""
        note = f"_{note_text}_"
        content = f"{partial}\n\n{note}" if partial else note

        await self._draw_stop(note_text, content)
        await self._remember(content, {"stopped": True})

    async def _draw_stop(self, note_text: str, content: str) -> None:
        """Отрисовка в ленте; при разрыве связи писать уже некому — это не сбой."""
        try:
            await self._tracer.stop_pending(note_text)
            await self._view.rewrite_answer(content, self._key)
        except Exception:
            logger.warning("stopped turn is not drawn: chat is gone", exc_info=True)

    async def _report_failure(self, error: BaseException) -> None:
        text = f"**сбой:** {error}"
        await self._view.error(text, self._key)
        await self._remember(text, {"error": True})

    async def _remember(self, content: str, marks: dict[str, Any]) -> None:
        """История обязана пережить остановку: её читает и лента, и сам агент."""
        await self._graph.aupdate_state(
            RunnableConfig(configurable={"thread_id": self._thread_id}),
            {"messages": [AIMessage(content=content, additional_kwargs=marks)]},
        )

    async def _on_chunk(self, chunk: BaseMessage) -> None:
        if not isinstance(chunk, AIMessageChunk):
            return
        if not isinstance(chunk.content, str) or not chunk.content:
            return
        await self._view.stream_answer(chunk.content, self._key)
