"""Состояние хода: исход выставляется один раз, артефакты стрима учтены."""

from __future__ import annotations

import pytest
from chainlit.step import Step

from boba.cancellation import StopReason
from boba.chainlit.chat.turn import TurnOutcome, TurnState, TurnStateError

pytestmark = pytest.mark.anyio


class TestOutcome:
    """Ровно один исход на запуск: первый settle побеждает."""

    async def test_first_settle_wins(self) -> None:
        state = TurnState()

        if state.settle_stopped(StopReason.USER_STOP) is not True:
            raise AssertionError("state.settle_stopped(StopReason.USER_STOP) is True")
        if state.settle_failed(RuntimeError("boom")) is not False:
            raise AssertionError('state.settle_failed(RuntimeError("boom")) is False')
        if state.settle_ok() is not False:
            raise AssertionError("state.settle_ok() is False")

        if state.outcome is not TurnOutcome.STOPPED:
            raise AssertionError("state.outcome is TurnOutcome.STOPPED")
        if state.error is not None:
            raise AssertionError("state.error is None")

    async def test_failed_keeps_the_error(self) -> None:
        state = TurnState()
        error = RuntimeError("boom")

        if state.settle_failed(error) is not True:
            raise AssertionError("state.settle_failed(error) is True")
        if state.outcome is not TurnOutcome.FAILED:
            raise AssertionError("state.outcome is TurnOutcome.FAILED")
        if state.error is not error:
            raise AssertionError("state.error is error")

    async def test_label_prefers_the_stop_reason(self) -> None:
        state = TurnState()
        state.settle_stopped(StopReason.ABORTED)

        if state.outcome_label != StopReason.ABORTED.value:
            raise AssertionError("state.outcome_label == StopReason.ABORTED.value")

    async def test_label_without_reason_is_generic(self) -> None:
        state = TurnState()
        state.settle_stopped(None)

        if state.outcome_label != TurnOutcome.STOPPED.value:
            raise AssertionError("state.outcome_label == TurnOutcome.STOPPED.value")

    async def test_label_before_settle_is_honest(self) -> None:
        if TurnState().outcome_label != "unsettled":
            raise AssertionError('TurnState().outcome_label == "unsettled"')

    async def test_second_run_is_a_protocol_violation(self) -> None:
        state = TurnState()
        state.begin()

        with pytest.raises(TurnStateError):
            state.begin()


class TestPendingArtifacts:
    """Шаги инструментов и рассуждения живут в состоянии до конца хода."""

    @staticmethod
    def _step(name: str) -> Step:
        return Step(name=name, type="tool")

    async def test_tools_open_close_and_drain(self) -> None:
        state = TurnState()
        first = self._step("first")
        second = self._step("second")

        state.open_tool("run-1", first)
        state.open_tool("run-2", second)
        if state.pending_tool_steps != [first, second]:
            raise AssertionError("state.pending_tool_steps == [first, second]")

        if state.close_tool("run-1") is not first:
            raise AssertionError('state.close_tool("run-1") is first')
        if state.close_tool("run-1") is not None:
            raise AssertionError('state.close_tool("run-1") is None')

        drained = list(state.drain_tools())
        if drained != [second]:
            raise AssertionError("drained == [second]")
        if state.pending_tool_steps != []:
            raise AssertionError("state.pending_tool_steps == []")

    async def test_reasoning_accumulates_per_run(self) -> None:
        state = TurnState()

        state.add_reasoning("run-1", "дум")
        state.add_reasoning("run-1", "аю")
        state.add_reasoning("run-2", "ещё")
        if state.pending_reasoning != "думаюещё":
            raise AssertionError('state.pending_reasoning == "думаюещё"')

        if state.take_reasoning("run-1") != "думаю":
            raise AssertionError('state.take_reasoning("run-1") == "думаю"')
        if state.take_reasoning("run-1") != "":
            raise AssertionError('state.take_reasoning("run-1") == ""')
        if state.pending_reasoning != "ещё":
            raise AssertionError('state.pending_reasoning == "ещё"')
