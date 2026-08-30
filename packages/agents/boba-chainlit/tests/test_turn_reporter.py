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
from uuid import UUID

import pytest
from conftest import RecordedTurn, use_context
from langchain_core.messages import BaseMessage

from boba.cancellation import StopReason
from boba.chainlit.chat.feed import TurnFeed
from boba.chainlit.chat.turn import (
    Question,
    TurnHistory,
    TurnMark,
    TurnRecord,
    TurnReporter,
    TurnState,
)
from boba.chainlit.domain.fields import StepField
from boba.chainlit.rendering.chat_view import ChatView, StepRole, StepStatus, StepText
from boba.identity.context import Scope
from boba.identity.errors import UserInputError
from boba.identity.locks import LockMode, LockPurpose, MemoryLiveLocks, RunLocking
from boba.messaging import (
    AnyMessage,
    LockToken,
    MemoryMessageBus,
    MemoryPayloadStore,
    MessageBusError,
)

pytestmark = pytest.mark.anyio

THREAD = "thread-reporter"
TURN_KEY = "msg-1"


class RememberedHistory(TurnHistory):
    """История в память: тесту важно, что и с какой пометкой записано.

    Перед записью уступает цикл событий, как настоящий I/O: отложенная отмена
    задачи хода обязана прилетать именно сюда.
    """

    def __init__(self) -> None:
        self.records: list[TurnRecord] = []

    async def remember(self, record: TurnRecord) -> None:
        await asyncio.sleep(0)
        self.records.append(record)


class BrokenBus(MemoryMessageBus):
    """Шина недоступна: любая публикация падает."""

    async def publish(self, scope: Scope, message: AnyMessage, token: LockToken) -> int:
        msg = f"bus is gone on {message.kind}"
        raise MessageBusError(msg)


def _chained_failure() -> Exception:
    try:
        try:
            raise OSError("connect refused")
        except OSError as low:
            raise RuntimeError("inference is unreachable") from low
    except RuntimeError as error:
        return error


def _reporter(
    feed: TurnFeed, state: TurnState, history: RememberedHistory
) -> TurnReporter:
    return TurnReporter(feed=feed, state=state, history=history, key=TURN_KEY)


def _turn() -> RecordedTurn:
    return RecordedTurn.recording(THREAD, TURN_KEY)


def _tool_step_id(call_id: str) -> str | None:
    return ChatView.derive_id(THREAD, call_id, StepRole.TOOL)


class TestFailed:
    """Сбой хода одинаково виден в ленте, истории и журнале."""

    async def test_three_channels_share_one_text(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        turn = _turn()
        state = TurnState()
        history = RememberedHistory()
        error = _chained_failure()

        with caplog.at_level(logging.ERROR):
            await _reporter(turn.feed, state, history).failed(error)

        described = "RuntimeError: inference is unreachable <- OSError: connect refused"

        error_steps = [s for s in turn.steps if s.get(StepField.IS_ERROR)]
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
        turn = _turn()
        state = TurnState()
        await turn.feed.tool_started("call-1", "web_search", {"query": "x"})
        state.open_tool("run-1", "call-1")

        await _reporter(turn.feed, state, RememberedHistory()).failed(
            RuntimeError("boom")
        )

        if state.pending_tool_calls != []:
            raise AssertionError("state.pending_tool_calls == []")
        step_id = _tool_step_id("call-1")
        closed = [s for s in turn.steps if s.get(StepField.ID) == step_id]
        if not (closed):
            raise AssertionError("closed")
        if closed[-1].get(StepField.OUTPUT) != StepText.TURN_FAILED.value:
            raise AssertionError("closed[-1].get(StepField.OUTPUT) == StepText.TURN_F…")
        if not (
            str(closed[-1].get(StepField.NAME)).startswith(StepStatus.FAILED.value)
        ):
            raise AssertionError("str(closed[-1].get(StepField.NAME)).startswith(Step…")

    async def test_user_input_error_stays_out_of_history(self) -> None:
        turn = _turn()
        history = RememberedHistory()

        await _reporter(turn.feed, TurnState(), history).failed(
            UserInputError("file is not supported")
        )

        if history.records != []:
            raise AssertionError("history.records == []")

    async def test_history_survives_a_broken_bus(self) -> None:
        history = RememberedHistory()
        broken = TurnFeed(
            BrokenBus("broken"),
            MemoryPayloadStore(),
            Scope.chat(THREAD),
            TURN_KEY,
            LockToken.local(),
        )

        with pytest.raises(MessageBusError, match="bus is gone"):
            await _reporter(broken, TurnState(), history).failed(RuntimeError("boom"))

        if len(history.records) != 1:
            raise AssertionError("len(history.records) == 1")
        if history.records[0].mark is not TurnMark.ERROR:
            raise AssertionError("history.records[0].mark is TurnMark.ERROR")


class TestStopped:
    """Остановка: частичный ответ с пометкой уходит и в ленту, и в историю."""

    async def test_partial_answer_is_kept_with_a_note(self) -> None:
        turn = _turn()
        state = TurnState()
        state.add_reasoning("run-1", "thinking hard")
        state.add_answer("partial text")
        await turn.feed.answer_token(TURN_KEY, "partial text")

        await _reporter(turn.feed, state, history := RememberedHistory()).stopped(
            StopReason.USER_STOP
        )

        expected = f"partial text\n\n_{StepText.STOPPED.value}_"
        answer = turn.view.answer_message
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
        turn = _turn()
        state = TurnState()
        await turn.feed.tool_started("call-1", "bash", {"cmd": "sleep 60"})
        state.open_tool("run-1", "call-1")

        await _reporter(turn.feed, state, RememberedHistory()).stopped(
            StopReason.USER_STOP
        )

        if state.pending_tool_calls != []:
            raise AssertionError("state.pending_tool_calls == []")
        step_id = _tool_step_id("call-1")
        closed = [s for s in turn.steps if s.get(StepField.ID) == step_id]
        if closed[-1].get(StepField.OUTPUT) != StepText.STOPPED.value:
            raise AssertionError("closed[-1].get(StepField.OUTPUT) == StepText.STOPPE…")


class TestFailedTurnKeepsHistory:
    """Регрессия: cancel(FAILED) отменяет задачу хода — отчёт обязан выжить.

    Прерыватель RunRegistry на cancel снимает задачу самого хода; если запись
    истории идёт после отмены, она молча гибнет на первом же await.
    """

    async def test_history_is_written_despite_the_cancellation(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from boba.chainlit.chat.turn import ChatTurn

        async def failing_stream() -> AsyncIterator[tuple[BaseMessage, dict[str, Any]]]:
            raise RuntimeError("inference is unreachable")
            yield

        recorded = _turn()
        history = RememberedHistory()
        turn = ChatTurn(
            thread_id=THREAD,
            feed=recorded.feed,
            history=cast(Any, history),
            question=Question(key=TURN_KEY, text="question"),
            locking=RunLocking(locks=MemoryLiveLocks("test:0", 20), heartbeat_sec=1.0),
        )

        # контекст вызова ставится до создания задачи: она копирует его при старте
        with use_context(monkeypatch, thread_id=THREAD).applied():
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
        turn = _turn()
        state = TurnState()
        await turn.feed.tool_started("call-1", "bash", {"cmd": "true"})
        state.open_tool("run-1", "call-1")

        await _reporter(turn.feed, state, history := RememberedHistory()).ok()

        if state.pending_tool_calls != []:
            raise AssertionError("state.pending_tool_calls == []")
        step_id = _tool_step_id("call-1")
        closed = [s for s in turn.steps if s.get(StepField.ID) == step_id]
        if closed[-1].get(StepField.OUTPUT) != StepText.FINISHED.value:
            raise AssertionError("closed[-1].get(StepField.OUTPUT) == StepText.FINISH…")
        if history.records != []:
            raise AssertionError("history.records == []")

    async def test_clean_finish_is_silent(self) -> None:
        turn = _turn()
        before = len(turn.steps)

        await _reporter(turn.feed, TurnState(), RememberedHistory()).ok()

        if len(turn.steps) != before:
            raise AssertionError("len(turn.steps) == before")


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
        self,
        stream: AsyncIterator[tuple[BaseMessage, dict[str, Any]]],
        monkeypatch: pytest.MonkeyPatch,
    ) -> ChatView:
        from boba.chainlit.chat.turn import ChatTurn

        recorded = _turn()
        turn = ChatTurn(
            thread_id=THREAD,
            feed=recorded.feed,
            history=cast(Any, RememberedHistory()),
            question=Question(key=TURN_KEY, text="question"),
            locking=RunLocking(locks=MemoryLiveLocks("test:0", 20), heartbeat_sec=1.0),
        )

        with use_context(monkeypatch, thread_id=THREAD).applied():
            task = asyncio.create_task(turn.run(stream))
            with contextlib.suppress(asyncio.CancelledError):
                await task

        return recorded.view

    async def test_finished_turn_clears_the_pulse(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        view = await self._run(self._silent_stream(), monkeypatch)

        if view.pulse_step is not None:
            raise AssertionError("finished turn leaves no pulse")

    async def test_failed_turn_clears_the_pulse(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        view = await self._run(self._failing_stream(), monkeypatch)

        if view.pulse_step is not None:
            raise AssertionError("failed turn leaves no pulse")


class TestBusyThread:
    """Занятый тред: ход не начинается, лента получает отказ с именем держателя."""

    async def test_turn_is_refused_while_another_holder_runs(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from boba.chainlit.chat.turn import ChatTurn

        async def silent_stream() -> AsyncIterator[tuple[BaseMessage, dict[str, Any]]]:
            return
            yield

        recorded = _turn()
        locks = MemoryLiveLocks("node2-chainlit", 20)
        scope = Scope.chat(THREAD)
        await locks.acquire(scope, LockMode.EXCLUSIVE, LockPurpose.TURN, UUID(int=1))
        history = RememberedHistory()
        turn = ChatTurn(
            thread_id=THREAD,
            feed=recorded.feed,
            history=cast(Any, history),
            question=Question(key=TURN_KEY, text="question"),
            locking=RunLocking(locks=locks, heartbeat_sec=1.0),
        )

        with use_context(monkeypatch, thread_id=THREAD).applied():
            await turn.run(silent_stream())

        errors = [s for s in recorded.steps if s.get(StepField.IS_ERROR)]
        if len(errors) != 1:
            raise AssertionError(f"one refusal in the feed: {recorded.steps}")
        if "node2-chainlit is running a turn" not in str(
            errors[0].get(StepField.OUTPUT)
        ):
            raise AssertionError(errors[0])
        if history.records != []:
            raise AssertionError("history.records == []")
        if recorded.view.pulse_step is not None:
            raise AssertionError("refused turn leaves no pulse")
