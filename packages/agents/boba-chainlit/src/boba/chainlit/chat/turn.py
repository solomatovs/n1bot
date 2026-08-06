"""Ход агента в чате: прогон стрима, фиксация остановки и точка её запуска.

Ход обрывается только по кнопке Stop: отмену держит TurnRegistry по thread_id,
и дотягивается до неё единственный обработчик. Разрыв связи ход не трогает —
пользователь мог закрыть вкладку, ответ он увидит в истории при возвращении.

Ошибки: наружу выходит только asyncio.CancelledError — её ждёт chainlit как
признак снятой задачи; прикладные сбои превращаются в сообщение об ошибке
в ленте и в истории.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from typing import Any, ClassVar

from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage
from langchain_core.runnables import RunnableConfig
from langgraph.graph.state import CompiledStateGraph

import chainlit as cl
from boba.cancellation import StopReason, ToolStopped, TurnRegistry
from boba.chainlit.chat.agent_tracer import AgentTracer
from boba.chainlit.rendering.chat_view import ChatView

__all__ = ["ChatTurn", "TurnReport", "TurnStopper"]

logger = logging.getLogger(__name__)


class TurnStopper:
    """Кнопка Stop: единственная точка остановки хода для callback'ов chainlit."""

    @staticmethod
    def stop(thread_id: str) -> bool:
        """Обрываем ход немедленно; False — останавливать нечего."""
        return TurnRegistry.instance().stop(thread_id, StopReason.USER_STOP)


class TurnReport:
    """Фиксация исхода хода: лента чата плюс история агента."""

    @staticmethod
    async def failure(
        graph: CompiledStateGraph,
        thread_id: str,
        view: ChatView,
        key: str,
        error: BaseException,
    ) -> None:
        text = f"**сбой:** {error}"
        await view.error(text, key)
        await TurnReport._remember(graph, thread_id, text, {"error": True})

    @staticmethod
    async def stopped(
        turn: ChatTurn,
        reason: StopReason | None,
    ) -> None:
        "фиксирует остановку: закрывает шаги и кладёт прерванный ответ в историю"
        graph, thread_id = turn.graph, turn.thread_id
        tracer, key, answer = turn.tracer, turn.key, turn.answer
        note_text = ChatView.stopped_text(reason)
        partial = (answer.content if answer else "") or ""
        note = f"_{note_text}_"
        content = f"{partial}\n\n{note}" if partial else note

        await TurnReport._draw_stop(tracer, key, answer, note_text, content)
        await TurnReport._remember(graph, thread_id, content, {"stopped": True})

    @staticmethod
    async def _draw_stop(
        tracer: AgentTracer,
        key: str,
        answer: cl.Message | None,
        note_text: str,
        content: str,
    ) -> None:
        """Отрисовка в ленте; при разрыве связи писать уже некому — это не сбой."""
        try:
            await tracer.stop_pending(note_text)
            if answer is not None:
                answer.content = content
                await answer.send()
            else:
                await tracer.view.answer(content, key)
        except Exception:
            logger.warning("stopped turn is not drawn: chat is gone", exc_info=True)

    @staticmethod
    async def _remember(
        graph: CompiledStateGraph,
        thread_id: str,
        content: str,
        marks: dict[str, Any],
    ) -> None:
        """История обязана пережить остановку: её читает и лента, и сам агент."""
        await graph.aupdate_state(
            RunnableConfig(configurable={"thread_id": thread_id}),
            {"messages": [AIMessage(content=content, additional_kwargs=marks)]},
        )


class ChatTurn:
    """Один ход: стрим ответа под отменой, зарегистрированной на thread_id."""

    _REPORTS: ClassVar[set[asyncio.Future[None]]] = set()
    """Живые отчёты об остановке: без ссылки задачу заберёт сборщик мусора."""

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
        self._answer: cl.Message | None = None

    @property
    def graph(self) -> CompiledStateGraph:
        return self._graph

    @property
    def thread_id(self) -> str:
        return self._thread_id

    @property
    def tracer(self) -> AgentTracer:
        return self._tracer

    @property
    def key(self) -> str:
        return self._key

    @property
    def answer(self) -> cl.Message | None:
        return self._answer

    async def run(
        self, stream: AsyncIterator[tuple[BaseMessage, dict[str, Any]]]
    ) -> None:
        """Гоняет стрим до конца либо до остановки; отменённый ход не молчит."""
        with TurnRegistry.instance().open(self._thread_id) as cancellation:
            try:
                async for chunk, _metadata in stream:
                    cancellation.raise_if_cancelled()
                    await self._on_chunk(chunk)
            except asyncio.CancelledError:
                # задачу сняли снаружи; после кнопки Stop причина уже своя
                cancellation.cancel(StopReason.ABORTED)
                await self._report_stop(cancellation.reason)
                raise
            except ToolStopped:
                await self._report_stop(cancellation.reason)
                return
            except Exception as e:
                logger.exception("agent run failed")
                cancellation.cancel(StopReason.FAILED)
                await TurnReport.failure(
                    self._graph, self._thread_id, self._view, self._key, e
                )
                return

        if self._answer is None:
            self._answer = self._view.open_answer(self._key)
        await self._answer.send()

    async def _report_stop(self, reason: StopReason | None) -> None:
        """Отчёт идёт своей задачей: повторная отмена хода не должна его съесть.

        Кнопку Stop chainlit обрабатывает отменой задачи хода, а прерыватель
        отменяет её второй раз — эта отмена приходит уже во время отрисовки.
        """
        report = asyncio.ensure_future(TurnReport.stopped(self, reason))
        self._REPORTS.add(report)
        report.add_done_callback(self._REPORTS.discard)
        await asyncio.shield(report)

    async def _on_chunk(self, chunk: BaseMessage) -> None:
        if not isinstance(chunk, AIMessageChunk):
            return
        if not isinstance(chunk.content, str) or not chunk.content:
            return
        if self._answer is None:
            self._answer = self._view.open_answer(self._key)
        await self._answer.stream_token(chunk.content)
