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

        assert state.settle_stopped(StopReason.USER_STOP) is True
        assert state.settle_failed(RuntimeError("boom")) is False
        assert state.settle_ok() is False

        assert state.outcome is TurnOutcome.STOPPED
        assert state.error is None

    async def test_failed_keeps_the_error(self) -> None:
        state = TurnState()
        error = RuntimeError("boom")

        assert state.settle_failed(error) is True
        assert state.outcome is TurnOutcome.FAILED
        assert state.error is error

    async def test_label_prefers_the_stop_reason(self) -> None:
        state = TurnState()
        state.settle_stopped(StopReason.ABORTED)

        assert state.outcome_label == StopReason.ABORTED.value

    async def test_label_without_reason_is_generic(self) -> None:
        state = TurnState()
        state.settle_stopped(None)

        assert state.outcome_label == TurnOutcome.STOPPED.value

    async def test_label_before_settle_is_honest(self) -> None:
        assert TurnState().outcome_label == "unsettled"

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
        assert state.pending_tool_steps == [first, second]

        assert state.close_tool("run-1") is first
        assert state.close_tool("run-1") is None

        drained = list(state.drain_tools())
        assert drained == [second]
        assert state.pending_tool_steps == []

    async def test_reasoning_accumulates_per_run(self) -> None:
        state = TurnState()

        state.add_reasoning("run-1", "дум")
        state.add_reasoning("run-1", "аю")
        state.add_reasoning("run-2", "ещё")
        assert state.pending_reasoning == "думаюещё"

        assert state.take_reasoning("run-1") == "думаю"
        assert state.take_reasoning("run-1") == ""
        assert state.pending_reasoning == "ещё"
