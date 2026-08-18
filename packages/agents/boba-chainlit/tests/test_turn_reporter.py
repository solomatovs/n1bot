"""Отчёт об исходе хода: чат, история и журнал получают одну формулировку.

Контракт: где бы ни упало, шаги закрыты, история помечена, а текст сбоя
в ленте дословно совпадает с историей и журналом.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from typing import Any, cast

import pytest
from langchain_core.messages import BaseMessage

from boba.cancellation import StopReason
from boba.chainlit.chat.turn import TurnMark, TurnRecord, TurnReporter, TurnState
from boba.chainlit.domain.errors import UserInputError
from boba.chainlit.domain.fields import StepField
from boba.chainlit.rendering.chat_view import (
    ChatView,
    RecordingSink,
    StepStatus,
    StepText,
)

pytestmark = pytest.mark.anyio

THREAD = "thread-reporter"
TURN_KEY = "msg-1"


class RememberedHistory:
    """История в память: тесту важно, что и с какой пометкой записано.

    Перед записью уступает цикл событий, как настоящий I/O: отложенная отмена
    задачи хода обязана прилетать именно сюда.
    """

    def __init__(self) -> None:
        self.records: list[TurnRecord] = []

    async def remember(self, record: TurnRecord) -> None:
        await asyncio.sleep(0)
        self.records.append(record)


class BrokenView:
    """Лента недоступна: любой вызов отрисовки падает."""

    def __getattr__(self, name: str) -> Any:
        async def boom(*args: Any, **kwargs: Any) -> None:
            raise RuntimeError(f"chat is gone on {name}")

        return boom


def _chained_failure() -> Exception:
    try:
        try:
            raise OSError("connect refused")
        except OSError as low:
            raise RuntimeError("inference is unreachable") from low
    except RuntimeError as error:
        return error


def _reporter(
    view: ChatView, state: TurnState, history: RememberedHistory
) -> TurnReporter:
    return TurnReporter(view=view, state=state, history=history, key=TURN_KEY)


async def _view_with_sink() -> tuple[ChatView, RecordingSink]:
    sink = RecordingSink()
    view = ChatView(THREAD, sink, user_name="tester")
    view.begin_turn(TURN_KEY)
    return view, sink


class TestFailed:
    """Сбой хода одинаково виден в ленте, истории и журнале."""

    async def test_three_channels_share_one_text(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        view, sink = await _view_with_sink()
        state = TurnState()
        history = RememberedHistory()
        error = _chained_failure()

        with caplog.at_level(logging.ERROR):
            await _reporter(view, state, history).failed(error)

        described = "RuntimeError: inference is unreachable <- OSError: connect refused"

        error_steps = [s for s in sink.steps if s.get(StepField.IS_ERROR)]
        if len(error_steps) != 1:
            raise AssertionError("len(error_steps) == 1")
        if error_steps[0].get(StepField.OUTPUT) != f"**failed:** {described}":
            raise AssertionError('error_steps[0].get(StepField.OUTPUT) == f"**failed:…')

        if len(history.records) != 1:
            raise AssertionError("len(history.records) == 1")
        if history.records[0].mark is not TurnMark.ERROR:
            raise AssertionError("history.records[0].mark is TurnMark.ERROR")
        if history.records[0].content != f"**failed:** {described}":
            raise AssertionError('history.records[0].content == f"**failed:** {descri…')

        logged = [r.message for r in caplog.records if "turn failed" in r.message]
        if not (logged):
            raise AssertionError("logged")
        if described not in logged[0]:
            raise AssertionError("described in logged[0]")

    async def test_pending_tool_steps_are_closed(self) -> None:
        view, sink = await _view_with_sink()
        state = TurnState()
        step = await view.tool_started("web_search", {"query": "x"}, "call-1")
        state.open_tool("run-1", step)

        await _reporter(view, state, RememberedHistory()).failed(RuntimeError("boom"))

        if state.pending_tool_steps != []:
            raise AssertionError("state.pending_tool_steps == []")
        closed = [s for s in sink.steps if s.get(StepField.ID) == step.id]
        if not (closed):
            raise AssertionError("closed")
        if closed[-1].get(StepField.OUTPUT) != StepText.TURN_FAILED.value:
            raise AssertionError("closed[-1].get(StepField.OUTPUT) == StepText.TURN_F…")
        if not (
            str(closed[-1].get(StepField.NAME)).startswith(StepStatus.FAILED.value)
        ):
            raise AssertionError("str(closed[-1].get(StepField.NAME)).startswith(Step…")

    async def test_user_input_error_stays_out_of_history(self) -> None:
        view, _sink = await _view_with_sink()
        history = RememberedHistory()

        await _reporter(view, TurnState(), history).failed(
            UserInputError("file is not supported")
        )

        if history.records != []:
            raise AssertionError("history.records == []")

    async def test_history_survives_a_broken_view(self) -> None:
        history = RememberedHistory()
        broken = cast("ChatView", BrokenView())

        await _reporter(broken, TurnState(), history).failed(RuntimeError("boom"))

        if len(history.records) != 1:
            raise AssertionError("len(history.records) == 1")
        if history.records[0].mark is not TurnMark.ERROR:
            raise AssertionError("history.records[0].mark is TurnMark.ERROR")


class TestStopped:
    """Остановка: частичный ответ с пометкой уходит и в ленту, и в историю."""

    async def test_partial_answer_is_kept_with_a_note(self) -> None:
        view, _sink = await _view_with_sink()
        state = TurnState()
        state.add_reasoning("run-1", "thinking hard")
        await view.stream_answer("partial text", TURN_KEY)

        await _reporter(view, state, history := RememberedHistory()).stopped(
            StopReason.USER_STOP
        )

        expected = f"partial text\n\n_{StepText.STOPPED.value}_"
        answer = view.answer_message
        if answer is None:
            raise AssertionError("answer is not None")
        if answer.content != expected:
            raise AssertionError("answer.content == expected")

        if len(history.records) != 1:
            raise AssertionError("len(history.records) == 1")
        record = history.records[0]
        if record.mark is not TurnMark.STOPPED:
            raise AssertionError("record.mark is TurnMark.STOPPED")
        if record.content != expected:
            raise AssertionError("record.content == expected")
        if record.reasoning != "thinking hard":
            raise AssertionError('record.reasoning == "thinking hard"')

    async def test_pending_tool_steps_are_closed_with_the_note(self) -> None:
        view, sink = await _view_with_sink()
        state = TurnState()
        step = await view.tool_started("bash", {"cmd": "sleep 60"}, "call-1")
        state.open_tool("run-1", step)

        await _reporter(view, state, RememberedHistory()).stopped(StopReason.USER_STOP)

        if state.pending_tool_steps != []:
            raise AssertionError("state.pending_tool_steps == []")
        closed = [s for s in sink.steps if s.get(StepField.ID) == step.id]
        if closed[-1].get(StepField.OUTPUT) != StepText.STOPPED.value:
            raise AssertionError("closed[-1].get(StepField.OUTPUT) == StepText.STOPPE…")


class TestFailedTurnKeepsHistory:
    """Регрессия: cancel(FAILED) отменяет задачу хода — отчёт обязан выжить.

    Прерыватель TurnContext на cancel снимает задачу самого хода; если запись
    истории идёт после отмены, она молча гибнет на первом же await.
    """

    async def test_history_is_written_despite_the_cancellation(self) -> None:
        from boba.chainlit.chat.turn import ChatTurn

        async def failing_stream() -> AsyncIterator[tuple[BaseMessage, dict[str, Any]]]:
            raise RuntimeError("inference is unreachable")
            yield

        view, _sink = await _view_with_sink()
        history = RememberedHistory()
        turn = ChatTurn(
            thread_id=THREAD,
            view=view,
            history=cast(Any, history),
            key=TURN_KEY,
        )

        task = asyncio.create_task(turn.run(failing_stream()))
        with contextlib.suppress(asyncio.CancelledError):
            await task

        if len(history.records) != 1:
            raise AssertionError("len(history.records) == 1")
        if history.records[0].mark is not TurnMark.ERROR:
            raise AssertionError("history.records[0].mark is TurnMark.ERROR")
        if "inference is unreachable" not in history.records[0].content:
            raise AssertionError('"inference is unreachable" in history.records[0].co…')


class TestOk:
    """Успех: забытые шаги закрываются, лишних записей истории нет."""

    async def test_leftover_steps_are_closed(self) -> None:
        view, sink = await _view_with_sink()
        state = TurnState()
        step = await view.tool_started("bash", {"cmd": "true"}, "call-1")
        state.open_tool("run-1", step)

        await _reporter(view, state, history := RememberedHistory()).ok()

        if state.pending_tool_steps != []:
            raise AssertionError("state.pending_tool_steps == []")
        closed = [s for s in sink.steps if s.get(StepField.ID) == step.id]
        if closed[-1].get(StepField.OUTPUT) != StepText.FINISHED.value:
            raise AssertionError("closed[-1].get(StepField.OUTPUT) == StepText.FINISH…")
        if history.records != []:
            raise AssertionError("history.records == []")

    async def test_clean_finish_is_silent(self) -> None:
        view, sink = await _view_with_sink()
        before = len(sink.steps)

        await _reporter(view, TurnState(), RememberedHistory()).ok()

        if len(sink.steps) != before:
            raise AssertionError("len(sink.steps) == before")


class TestPulseOfTheTurn:
    """Кружок ожидания снимается любым исходом хода, а не только удачным."""

    @staticmethod
    async def _silent_stream() -> AsyncIterator[tuple[BaseMessage, dict[str, Any]]]:
        """Ход без единого токена: кружок к финалу остаётся показанным."""
        return
        yield

    @staticmethod
    async def _failing_stream() -> AsyncIterator[tuple[BaseMessage, dict[str, Any]]]:
        raise RuntimeError("inference is unreachable")
        yield

    async def _run(
        self, stream: AsyncIterator[tuple[BaseMessage, dict[str, Any]]]
    ) -> ChatView:
        from boba.chainlit.chat.turn import ChatTurn

        view, _sink = await _view_with_sink()
        turn = ChatTurn(
            thread_id=THREAD,
            view=view,
            history=cast(Any, RememberedHistory()),
            key=TURN_KEY,
        )

        task = asyncio.create_task(turn.run(stream))
        with contextlib.suppress(asyncio.CancelledError):
            await task

        return view

    async def test_finished_turn_clears_the_pulse(self) -> None:
        view = await self._run(self._silent_stream())

        if view.pulse_step is not None:
            raise AssertionError("finished turn leaves no pulse")

    async def test_failed_turn_clears_the_pulse(self) -> None:
        view = await self._run(self._failing_stream())

        if view.pulse_step is not None:
            raise AssertionError("failed turn leaves no pulse")
