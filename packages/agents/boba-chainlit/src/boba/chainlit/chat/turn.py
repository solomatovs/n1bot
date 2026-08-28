"""Один ход чата: состояние с однократным исходом (TurnState), отчёт об исходе в
шину, историю и журнал (TurnReporter), стрим ответа под отменой RunRegistry
(ChatTurn).

Ошибки:
asyncio.CancelledError — ход снят; её ждёт chainlit как признак отмены задачи.
TurnStateError — нарушен протокол хода: повторный запуск одного ChatTurn.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from abc import abstractmethod
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, ClassVar, Protocol

from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage, HumanMessage

import chainlit as cl
from boba.cancellation import StopReason, ToolStopped
from boba.canvas.keys import ObjectKey
from boba.chainlit.chat.feed import TurnFeed
from boba.chainlit.chat.tracing import AgentTracer, TurnArtifacts
from boba.chainlit.rendering.chat_view import ChatView, StepRole, StepText
from boba.identity.context import CallContext
from boba.identity.errors import FailureReport, RefusalError
from boba.identity.locks import (
    LockBusyError,
    LockKeeper,
    LockMode,
    LockPurpose,
    RunLocking,
)
from boba.identity.run import ElementTarget, RunPort, RunRefusal, RunRegistry
from boba.llm.chat import ResponseField
from boba.messaging import NoticeLevel, TurnOutcome

__all__ = [
    "ChatTurn",
    "TurnHistory",
    "TurnMark",
    "TurnOutcome",
    "TurnRecord",
    "TurnReporter",
    "TurnState",
    "TurnStateError",
]

logger = logging.getLogger(__name__)


class TurnMark(StrEnum):
    """Пометка исхода хода в additional_kwargs сообщения истории; по ней сборка ленты из
    истории рисует остановленный или упавший ход.
    """

    STOPPED = "stopped"
    ERROR = "error"


@dataclass
class TurnRecord:
    """Запись оборванного хода для истории агента: текст ответа, пометка исхода и
    незавершённые рассуждения, чтобы модель знала, чем ход кончился.
    """

    content: str
    mark: TurnMark
    reasoning: str = ""

    def message(self) -> AIMessage:
        """Собирает сообщение для состояния графа: текст, пометка исхода и рассуждения,
        если они были.
        """
        extra: dict[str, Any] = {self.mark.value: True}
        if self.reasoning:
            extra[ResponseField.REASONING_CONTENT.value] = self.reasoning

        return AIMessage(content=self.content, additional_kwargs=extra)


class TurnStateError(Exception):
    """Нарушение протокола хода: один ChatTurn запущен дважды."""


class TurnState(TurnArtifacts):
    """Состояние хода: исход, зафиксированный ровно один раз, незакрытые вызовы
    инструментов, потоковые рассуждения и накопленный текст ответа.
    """

    def __init__(self) -> None:
        self._began = False
        self._outcome: TurnOutcome | None = None
        self._reason: StopReason | None = None
        self._error: BaseException | None = None
        self._tool_calls: dict[str, str] = {}
        self._reasoning: dict[str, str] = {}
        self._answer: list[str] = []

    def begin(self) -> None:
        """Отмечает запуск хода; второй запуск того же хода — ошибка программиста."""
        if self._began:
            raise TurnStateError("turn already ran")

        self._began = True

    @property
    def outcome(self) -> TurnOutcome | None:
        """Исход хода; None, пока ход не завершён."""
        return self._outcome

    @property
    def outcome_label(self) -> str:
        """Метка исхода для журнала: у остановки причина точнее самого исхода."""
        if self._outcome is None:
            return "unsettled"

        if self._outcome is TurnOutcome.STOPPED and self._reason is not None:
            return self._reason.value

        return self._outcome.value

    @property
    def error(self) -> BaseException | None:
        """Ошибка, зафиксированная исходом FAILED."""
        return self._error

    @property
    def reason(self) -> StopReason | None:
        """Причина остановки, зафиксированная исходом STOPPED."""
        return self._reason

    def settle_ok(self) -> bool:
        """Фиксирует успешное завершение; False — исход уже определён."""
        return self._settle(TurnOutcome.OK)

    def settle_stopped(self, reason: StopReason | None) -> bool:
        """Фиксирует остановку; False — исход уже определён."""
        settled = self._settle(TurnOutcome.STOPPED)
        if settled:
            self._reason = reason

        return settled

    def settle_failed(self, error: BaseException) -> bool:
        """Фиксирует сбой; False — исход уже определён."""
        settled = self._settle(TurnOutcome.FAILED)
        if settled:
            self._error = error

        return settled

    def _settle(self, outcome: TurnOutcome) -> bool:
        if self._outcome is not None:
            logger.debug(
                "outcome already settled as %s, %s ignored",
                self._outcome.value,
                outcome.value,
            )
            return False

        self._outcome = outcome
        return True

    def open_tool(self, run_key: str, call_id: str) -> None:
        """Запоминает вызов запущенного инструмента по ключу прогона langchain."""
        self._tool_calls[run_key] = call_id

    def close_tool(self, run_key: str) -> str | None:
        """Снимает с учёта вызов завершённого инструмента и возвращает его call_id;
        None, если прогон неизвестен.
        """
        return self._tool_calls.pop(run_key, None)

    def drain_tools(self) -> Iterator[str]:
        """Отдаёт и снимает с учёта все незавершённые вызовы инструментов."""
        while self._tool_calls:
            _, call_id = self._tool_calls.popitem()
            yield call_id

    @property
    def pending_tool_calls(self) -> list[str]:
        """Вызовы инструментов, которые ход ещё не завершил."""
        return list(self._tool_calls.values())

    def add_answer(self, text: str) -> None:
        """Копит текст ответа из стрима, чтобы прерванный ответ попал в историю."""
        self._answer.append(text)

    @property
    def answer_text(self) -> str:
        """Текст ответа, накопленный к этому моменту."""
        return "".join(self._answer)

    def add_reasoning(self, run_key: str, text: str) -> None:
        """Копит потоковые рассуждения прогона по его ключу."""
        self._reasoning[run_key] = self._reasoning.get(run_key, "") + text

    def take_reasoning(self, run_key: str) -> str:
        """Забирает накопленные рассуждения прогона; пустая строка, если их не было."""
        return self._reasoning.pop(run_key, "")

    @property
    def pending_reasoning(self) -> str:
        """Рассуждения незавершённых прогонов; после остановки их получает история."""
        parts: list[str] = []
        for text in self._reasoning.values():
            parts.append(text)

        return "".join(parts)


class TurnHistory(Protocol):
    """Порт истории хода: записывает исход так, чтобы он пережил остановку и был виден
    модели в следующем ходе.
    """

    @abstractmethod
    async def remember(self, record: TurnRecord) -> None: ...


class TurnReporter:
    """Отчёт об исходе хода в шину, историю агента и журнал из одного разбора
    FailureReport.
    """

    def __init__(
        self,
        feed: TurnFeed,
        state: TurnState,
        history: TurnHistory,
        key: str,
    ) -> None:
        self._feed = feed
        self._state = state
        self._history = history
        self._key = key

    async def ok(self) -> None:
        """Успешное завершение: забытые вызовы инструментов закрываются, чтобы не висеть
        «в процессе».
        """
        leftovers = list(self._state.drain_tools())
        if not leftovers:
            return

        logger.warning("turn finished ok with %d unclosed tool calls", len(leftovers))

        for call_id in leftovers:
            await self._feed.tool_stopped(call_id, StepText.FINISHED.value)

    async def stopped(self, reason: StopReason | None) -> None:
        """Остановка хода: закрывает вызовы, помечает ответ в ленте и пишет
        прерванный ответ в историю.
        """
        note = str(StepText.for_stop(reason))
        content = self._interrupted_answer(note)

        try:
            await self._draw_stop(note)
        finally:
            await self._remember(content, TurnMark.STOPPED)

    async def failed(self, error: BaseException) -> None:
        """Сбой хода: чат, история и журнал получают формулировку одного разбора."""
        report = FailureReport.of(error)
        logger.error("turn failed: %s", report.log, exc_info=error)

        try:
            await self._draw_failure(report)
        finally:
            if report.history:
                await self._remember(f"**failed:** {report.history}", TurnMark.ERROR)

    def _interrupted_answer(self, note_text: str) -> str:
        """Собирает текст прерванного ответа: накопленный стрим и курсивная пометка."""
        partial = self._state.answer_text

        note = f"_{note_text}_"
        if not partial:
            return note

        return f"{partial}\n\n{note}"

    async def _draw_stop(self, note_text: str) -> None:
        """Публикует сообщения об остановке: рассуждения закрыты, вызовы сняты, ответ
        помечен.
        """
        await self._feed.thinking_closed()

        for call_id in self._state.drain_tools():
            await self._feed.tool_stopped(call_id, note_text)

        await self._feed.answer_interrupted(self._key, note_text)

    async def _draw_failure(self, report: FailureReport) -> None:
        """Публикует сообщения о сбое: рассуждения закрыты, вызовы сняты, текст сбоя
        показан.
        """
        await self._feed.thinking_closed()

        for call_id in self._state.drain_tools():
            await self._feed.tool_stopped(call_id, StepText.TURN_FAILED.value)

        if report.view:
            await self._feed.notice(NoticeLevel.ERROR, f"**failed:** {report.view}")

    async def _remember(self, content: str, mark: TurnMark) -> None:
        """Пишет запись исхода в историю; её читает и лента, и сам агент."""
        record = TurnRecord(
            content=content,
            mark=mark,
            reasoning=self._state.pending_reasoning,
        )
        await self._history.remember(record)


class ChatTurn(RunPort):
    """Один ход чата: гонит стрим ответа под отменой RunRegistry, публикует события
    через TurnFeed и отчитывается ровно одним исходом.
    """

    _REPORTS: ClassVar[set[asyncio.Future[None]]] = set()
    """Живые отчёты об остановке: без ссылки задачу заберёт сборщик мусора."""

    def __init__(
        self,
        thread_id: str,
        feed: TurnFeed,
        history: TurnHistory,
        key: str,
        locking: RunLocking,
    ) -> None:
        self._thread_id = thread_id
        self._feed = feed
        self._key = key
        self._locks = locking.locks
        self._heartbeat_sec = locking.heartbeat_sec
        self._state = TurnState()
        self._answered = False
        self._tracer = AgentTracer(feed, self._state)
        self._reporter = TurnReporter(
            feed=feed,
            state=self._state,
            history=history,
            key=key,
        )

    @property
    def tracer(self) -> AgentTracer:
        """Трасер хода; его отдают в callbacks прогона графа."""
        return self._tracer

    @classmethod
    def stop(cls, thread_id: str) -> bool:
        """Обрывает живой ход треда по кнопке Stop; False, если останавливать нечего."""
        return RunRegistry.stop(thread_id, StopReason.USER_STOP)

    @classmethod
    def active(cls, thread_id: str) -> ChatTurn | None:
        """Возвращает живой ход треда; None, если тред ничем не занят."""
        turn = RunRegistry.port_of(thread_id)
        if not isinstance(turn, ChatTurn):
            return None

        return turn

    def element_target(self, tool_call_id: str) -> ElementTarget:
        """Возвращает адрес элемента вызова инструмента: он крепится к шагу ответа."""
        for_id = ChatView.derive_id(self._thread_id, self._key, StepRole.ANSWER)
        if not for_id:
            raise RefusalError(RunRefusal.NO_TURN, "the turn has no answer step")

        element_id = ChatView.derive_id(self._thread_id, tool_call_id, StepRole.ELEMENT)
        if not element_id:
            raise RefusalError(RunRefusal.NO_TOOL_CALL, "tool call without id")

        return ElementTarget(for_id=for_id, element_id=element_id)

    @staticmethod
    def human_message(msg: cl.Message, user_id: str) -> HumanMessage:
        """Собирает сообщение пользователя для графа; пути вложений — такие, какими их
        видит песочница.
        """
        attachments: list[dict[str, str]] = []

        for element in msg.elements or []:
            key = ObjectKey.build(user_id, element.thread_id, element.name, element.id)
            name = element.name
            if not name:
                name = element.id

            attachments.append({"name": name, "path": key.in_workspace()})
        extra: dict[str, Any] = {}
        if attachments:
            extra = {"attachments": attachments}
        return HumanMessage(content=msg.content, id=msg.id, additional_kwargs=extra)

    async def run(
        self, stream: AsyncIterator[tuple[BaseMessage, dict[str, Any]]]
    ) -> None:
        """Гонит стрим до конца либо до остановки и отчитывается исходом; отменённый ход
        не молчит.
        """
        self._state.begin()
        started = time.monotonic()
        logger.info("turn start: thread=%s key=%s", self._thread_id, self._key)

        context = CallContext.current()
        try:
            lock = await self._locks.acquire(
                context.scope,
                LockMode.EXCLUSIVE,
                LockPurpose.TURN,
                context.subject.user_id,
            )
        except LockBusyError as exc:
            # ход не начат: ленте достаточно уведомления, истории — нечего помнить
            logger.warning("turn refused: %s", exc)
            self._state.settle_failed(exc)
            await self._feed.notice(NoticeLevel.ERROR, str(exc))
            return

        self._feed.adopt(lock.token)
        keeper = LockKeeper(
            self._locks, lock, context.cancellation, self._heartbeat_sec
        )
        async with keeper:
            try:
                await self._run(stream)
            finally:
                elapsed_ms = int((time.monotonic() - started) * 1000)
                logger.info(
                    "turn finished: thread=%s outcome=%s in %dms",
                    self._thread_id,
                    self._state.outcome_label,
                    elapsed_ms,
                )
                await self._finish_ui()

    async def crash(self, error: BaseException) -> None:
        """Отчёт о сбое вокруг хода — до стрима или после него — теми же тремя
        каналами.
        """
        if self._state.settle_failed(error):
            await self._reporter.failed(error)
            await self._finish_ui()
            return

        raise error

    async def _run(
        self, stream: AsyncIterator[tuple[BaseMessage, dict[str, Any]]]
    ) -> None:
        context = CallContext.current()
        cancellation = context.cancellation
        with RunRegistry.open(context, self), RunRegistry.task_abort(cancellation):
            try:
                # ход объявляется до первого чанка: запрос в модель уходит с первой
                # итерацией стрима, и до её ответа лента иначе пуста
                await self._feed.started(self._key)
                async for chunk, _metadata in stream:
                    cancellation.raise_if_cancelled()
                    await self._model_answered()
                    await self._on_chunk(chunk)

                await self._feed.answer_closed(self._key)
            except asyncio.CancelledError:
                # задачу сняли снаружи; после кнопки Stop причина уже своя
                cancellation.cancel(StopReason.ABORTED)
                if self._state.settle_stopped(cancellation.reason):
                    await self._report_stop(cancellation.reason)
                raise
            except ToolStopped:
                if self._state.settle_stopped(cancellation.reason):
                    await self._report_stop(cancellation.reason)
                return
            except Exception as e:
                # отчёт до cancel: отмена гасит и задачу самого хода, незащищённый
                # await после неё умирает — история сбоя была бы потеряна
                if self._state.settle_failed(e):
                    await self._reporter.failed(e)
                cancellation.cancel(StopReason.FAILED)
                return

        if self._state.settle_ok():
            await self._reporter.ok()

    async def _finish_ui(self) -> None:
        """Публикует TurnFinished: получатели гасят кружок ожидания и индикатор хода
        во всех вкладках треда.
        """
        outcome = self._state.outcome
        if outcome is None:
            outcome = TurnOutcome.FAILED

        end = asyncio.ensure_future(self._feed.finished(outcome, self._finish_reason()))
        self._REPORTS.add(end)
        end.add_done_callback(self._REPORTS.discard)
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.shield(end)

    def _finish_reason(self) -> str:
        """Текст исхода для получателей: причина остановки или формулировка сбоя."""
        if self._state.outcome is TurnOutcome.STOPPED:
            return str(StepText.for_stop(self._state.reason))

        error = self._state.error
        if error is None:
            return ""

        view = FailureReport.of(error).view
        if view is None:
            return ""

        return view

    async def _model_answered(self) -> None:
        """Публикует ModelAnswered на первом чанке: получатели снимают пометку ожидания
        запроса.
        """
        if self._answered:
            return

        self._answered = True
        await self._feed.model_answered()

    async def _report_stop(self, reason: StopReason | None) -> None:
        """Отчёт об остановке идёт своей задачей, чтобы повторная отмена хода его не
        съела.
        """
        report = asyncio.ensure_future(self._reporter.stopped(reason))
        self._REPORTS.add(report)
        report.add_done_callback(self._REPORTS.discard)
        await asyncio.shield(report)

    async def _on_chunk(self, chunk: BaseMessage) -> None:
        if not isinstance(chunk, AIMessageChunk):
            return

        if not isinstance(chunk.content, str):
            return

        if not chunk.content:
            return

        self._state.add_answer(chunk.content)
        await self._feed.answer_token(self._key, chunk.content)
