"""Система хода: состояние, отчёт об исходе и жизненный цикл.

Исход хода фиксируется в TurnState ровно один раз: первый settle побеждает,
повторные не порождают второго отчёта. Отчёт по исходу — чат, история агента,
журнал сервера — делает TurnReporter из одного разбора FailureReport; любой
исход закрывает незавершённые шаги, запись истории не зависит от ленты.
ChatTurn гоняет стрим под отменой RunRegistry и отчитывается ровно одним
исходом; crash() покрывает сбои вокруг стрима — до него и после.

Ход обрывается только по кнопке Stop: отмену держит RunRegistry по thread_id.
Разрыв связи ход не трогает — ответ пользователь увидит в истории.

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
from boba.chainlit.chat.tracing import AgentTracer, TurnArtifacts
from boba.chainlit.domain.context import CallContext
from boba.chainlit.domain.errors import FailureReport, RefusalError
from boba.chainlit.domain.fields import StepField, ThreadField
from boba.chainlit.domain.keys import ObjectKey
from boba.chainlit.domain.run import ElementTarget, RunPort, RunRefusal, RunRegistry
from boba.chainlit.rendering.chat_view import ChatView, StepRole, StepText
from boba.llm.chat import ResponseField
from chainlit.step import Step, StepDict
from chainlit.types import ThreadDict

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
    """Исход хода в additional_kwargs: его читает сборка ленты из истории."""

    STOPPED = "stopped"
    ERROR = "error"


@dataclass
class TurnRecord:
    """Запись оборванного хода в историю агента."""

    content: str
    mark: TurnMark
    reasoning: str = ""

    def message(self) -> AIMessage:
        """Сообщение для состояния графа: пометка исхода и рассуждения при них."""
        extra: dict[str, Any] = {self.mark.value: True}
        if self.reasoning:
            extra[ResponseField.REASONING_CONTENT.value] = self.reasoning

        return AIMessage(content=self.content, additional_kwargs=extra)


class TurnStateError(Exception):
    """Нарушение протокола хода: состояние использовано не по контракту."""


class TurnOutcome(StrEnum):
    """Исход хода; ровно один на запуск."""

    OK = "ok"
    STOPPED = "stopped"
    FAILED = "failed"


class TurnState(TurnArtifacts):
    """Состояние хода: исход-машина set-once и незакрытые артефакты стрима."""

    def __init__(self) -> None:
        self._began = False
        self._outcome: TurnOutcome | None = None
        self._reason: StopReason | None = None
        self._error: BaseException | None = None
        self._tool_steps: dict[str, Step] = {}
        self._reasoning: dict[str, str] = {}

    def begin(self) -> None:
        """Отмечает запуск; второй запуск того же хода — ошибка программиста."""
        if self._began:
            raise TurnStateError("turn already ran")

        self._began = True

    @property
    def outcome(self) -> TurnOutcome | None:
        """Исход хода; None — ход ещё не завершён."""
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

    def open_tool(self, run_key: str, step: Step) -> None:
        """Регистрирует шаг запущенного инструмента по ключу прогона."""
        self._tool_steps[run_key] = step

    def close_tool(self, run_key: str) -> Step | None:
        """Забирает шаг завершённого инструмента; None — прогон неизвестен."""
        return self._tool_steps.pop(run_key, None)

    def drain_tools(self) -> Iterator[Step]:
        """Отдаёт и снимает с учёта все незавершённые шаги инструментов."""
        while self._tool_steps:
            _, step = self._tool_steps.popitem()
            yield step

    @property
    def pending_tool_steps(self) -> list[Step]:
        """Шаги инструментов, ещё не завершённые ходом."""
        return list(self._tool_steps.values())

    def add_reasoning(self, run_key: str, text: str) -> None:
        """Копит потоковые рассуждения прогона."""
        self._reasoning[run_key] = self._reasoning.get(run_key, "") + text

    def take_reasoning(self, run_key: str) -> str:
        """Забирает накопленные рассуждения прогона; пусто — их не было."""
        return self._reasoning.pop(run_key, "")

    @property
    def pending_reasoning(self) -> str:
        """Рассуждения незавершённых прогонов: их ждёт история после остановки."""
        parts: list[str] = []
        for text in self._reasoning.values():
            parts.append(text)

        return "".join(parts)


class TurnHistory(Protocol):
    """Порт истории хода: запись исхода, который переживёт остановку."""

    @abstractmethod
    async def remember(self, record: TurnRecord) -> None: ...


class TurnReporter:
    """Исход хода тремя каналами: лента, история агента, журнал сервера."""

    def __init__(
        self,
        view: ChatView,
        state: TurnState,
        history: TurnHistory,
        key: str,
    ) -> None:
        self._view = view
        self._state = state
        self._history = history
        self._key = key

    async def ok(self) -> None:
        """Успех: забытые шаги закрываются, а не висят «в процессе»."""
        leftovers = list(self._state.drain_tools())
        if not leftovers:
            return

        logger.warning("turn finished ok with %d unclosed tool steps", len(leftovers))

        for step in leftovers:
            await self._view.tool_stopped(step, StepText.FINISHED.value)

    async def stopped(self, reason: StopReason | None) -> None:
        """Остановка: пометка в ленте и прерванный ответ в истории."""
        note = str(StepText.for_stop(reason))
        content = self._interrupted_answer(note)

        await self._draw_stop(note, content)
        await self._remember(content, TurnMark.STOPPED)

    async def failed(self, error: BaseException) -> None:
        """Сбой: чат, история и журнал получают формулировку одного разбора."""
        report = FailureReport.of(error)
        logger.error("turn failed: %s", report.log, exc_info=error)

        await self._draw_failure(report)

        if report.history:
            await self._remember(f"**failed:** {report.history}", TurnMark.ERROR)

    def _interrupted_answer(self, note_text: str) -> str:
        """Текст прерванного ответа: накопленный стрим плюс курсивная пометка."""
        partial = ""
        answer = self._view.answer_message
        if answer is not None and answer.content:
            partial = answer.content

        note = f"_{note_text}_"
        if not partial:
            return note

        return f"{partial}\n\n{note}"

    async def _draw_stop(self, note_text: str, content: str) -> None:
        """Отрисовка в ленте; при разрыве связи писать уже некому — это не сбой."""
        try:
            await self._view.close_thinking()

            for step in self._state.drain_tools():
                await self._view.tool_stopped(step, note_text)

            await self._view.rewrite_answer(content, self._key)
        except Exception:
            logger.warning("stopped turn is not drawn: chat is gone", exc_info=True)

    async def _draw_failure(self, report: FailureReport) -> None:
        """Отрисовка сбоя; недоступная лента не должна терять запись истории."""
        try:
            await self._view.close_thinking()

            for step in self._state.drain_tools():
                await self._view.tool_stopped(step, StepText.TURN_FAILED.value)

            if report.view:
                await self._view.error(f"**failed:** {report.view}", self._key)
        except Exception:
            logger.warning("failed turn is not drawn: chat is gone", exc_info=True)

    async def _remember(self, content: str, mark: TurnMark) -> None:
        """История обязана пережить исход: её читает и лента, и сам агент."""
        record = TurnRecord(
            content=content,
            mark=mark,
            reasoning=self._state.pending_reasoning,
        )
        await self._history.remember(record)


class ChatTurn(RunPort):
    """Один ход: стрим ответа под отменой, зарегистрированной на thread_id."""

    _REPORTS: ClassVar[set[asyncio.Future[None]]] = set()
    """Живые отчёты об остановке: без ссылки задачу заберёт сборщик мусора."""

    def __init__(
        self,
        thread_id: str,
        view: ChatView,
        history: TurnHistory,
        key: str,
    ) -> None:
        self._thread_id = thread_id
        self._view = view
        self._key = key
        self._state = TurnState()
        self._tracer = AgentTracer(view, self._state)
        self._reporter = TurnReporter(
            view=view,
            state=self._state,
            history=history,
            key=key,
        )

    @property
    def tracer(self) -> AgentTracer:
        """Трасер хода: его отдают в callbacks прогона графа."""
        return self._tracer

    @classmethod
    def stop(cls, thread_id: str) -> bool:
        """Кнопка Stop: обрывает ход немедленно; False — останавливать нечего."""
        return RunRegistry.stop(thread_id, StopReason.USER_STOP)

    @classmethod
    def active(cls, thread_id: str) -> ChatTurn | None:
        """Живой ход треда; None — тред ничем не занят."""
        turn = RunRegistry.port_of(thread_id)
        if not isinstance(turn, ChatTurn):
            return None

        return turn

    def element_target(self, tool_call_id: str) -> ElementTarget:
        """Элемент вызова крепится к шагу ответа хода."""
        for_id = ChatView.derive_id(self._thread_id, self._key, StepRole.ANSWER)
        if not for_id:
            raise RefusalError(RunRefusal.NO_TURN, "the turn has no answer step")

        element_id = ChatView.derive_id(self._thread_id, tool_call_id, StepRole.ELEMENT)
        if not element_id:
            raise RefusalError(RunRefusal.NO_TOOL_CALL, "tool call without id")

        return ElementTarget(for_id=for_id, element_id=element_id)

    @staticmethod
    def human_message(msg: cl.Message, user_id: str) -> HumanMessage:
        """Сообщение пользователя; пути вложений — как их видит песочница."""
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

    def resume_steps(self) -> list[StepDict]:
        """Незавершённые шаги хода: в истории их ещё нет, вкладке нужны сразу."""
        steps: list[StepDict] = []
        if container := self._view.container_step:
            steps.append(container.to_dict())

        if thinking := self._view.thinking_step:
            steps.append(thinking.to_dict())

        for step in self._state.pending_tool_steps:
            steps.append(step.to_dict())

        if answer := self._view.answer_message:
            steps.append(answer.to_dict())

        if pulse := self._view.pulse_step:
            steps.append(pulse.to_dict())

        return steps

    def resume_into(self, thread_dict: ThreadDict) -> None:
        """Подкладывает живые шаги в ленту треда до её отправки клиенту.

        Шаг с тем же id замещается — вкладка получает свежее состояние стрима,
        новые шаги дописываются в конец.
        """
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

    async def run(
        self, stream: AsyncIterator[tuple[BaseMessage, dict[str, Any]]]
    ) -> None:
        """Гоняет стрим до конца либо до остановки; отменённый ход не молчит."""
        self._state.begin()
        started = time.monotonic()
        logger.info("turn start: thread=%s key=%s", self._thread_id, self._key)
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
        """Сбой вокруг хода — до стрима или после: тот же трёхканальный отчёт.

        Если исход уже был определён, второго отчёта не будет — ошибка идёт
        наверх, её покажет обёртка callback'а.
        """
        if self._state.settle_failed(error):
            await self._reporter.failed(error)
            return

        raise error

    async def _run(
        self, stream: AsyncIterator[tuple[BaseMessage, dict[str, Any]]]
    ) -> None:
        context = CallContext.current()
        cancellation = context.cancellation
        with RunRegistry.open(context, self), RunRegistry.task_abort(cancellation):
            try:
                await cl.context.emitter.task_start()
                # контейнер до первого чанка: запрос в модель уходит с первой
                # итерацией стрима, и до её ответа лента иначе пуста
                await self._view.await_model()
                async for chunk, _metadata in stream:
                    cancellation.raise_if_cancelled()
                    await self._view.model_answered()
                    await self._on_chunk(chunk)

                await self._view.close_answer(self._key)
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
        """Гасит кружок ожидания и loading во всех вкладках треда: chainlit шлёт
        task_end только сессии, начавшей ход, а после F5 она мертва."""
        try:
            await self._view.finish_turn()
        except Exception:
            logger.warning("turn pulse is not cleared: chat is gone", exc_info=True)

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
        report = asyncio.ensure_future(self._reporter.stopped(reason))
        self._REPORTS.add(report)
        report.add_done_callback(self._REPORTS.discard)
        await asyncio.shield(report)

    async def _on_chunk(self, chunk: BaseMessage) -> None:
        if not isinstance(chunk, AIMessageChunk):
            return
        if not isinstance(chunk.content, str) or not chunk.content:
            return
        await self._view.stream_answer(chunk.content, self._key)
