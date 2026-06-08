"""Контракт AgentLoop: дренаж круга до StopIteration вместо обрыва.

Суть отличия от обрыва (`return` посреди `for`): при срабатывании stop_if
inner-генератор НЕ бросается недочитанным (что дало бы GeneratorExit и метку
cancelled на терминале), а дочитывается до естественного StopIteration.
"""

from __future__ import annotations

from collections.abc import Iterable

from boba.agent.agent import AgentContext
from boba.agent.events import (
    AgentEvent,
    GenerationFailed,
    ToolCallMessage,
    TotalMessage,
)
from boba.agent.loop import AgentLoop
from boba.agent.middleware.loop_control import StopIfReasonStop, StopOnAnyFailure
from boba.llm.events import FinishReason
from boba.llm.models import AssistantMessage, RequestId, ToolCall, new_request_id
from boba.patterns import StreamSource


class _RecordingSource(StreamSource[AgentContext, AgentEvent]):
    """Inner-стрим: эмитит заданные раунды, фиксируя способ завершения каждого.

    completed — раунды, дочитанные до StopIteration (штатно).
    gen_exited — раунды, закрытые через GeneratorExit (обрыв).
    """

    def __init__(self, rounds: list[list[AgentEvent]]) -> None:
        self._rounds = rounds
        self._round = 0
        self.completed: list[int] = []
        self.gen_exited: list[int] = []

    def name(self) -> str:
        return "RecordingSource"

    def reset(self) -> None:
        pass

    def stream(self, ctx: AgentContext) -> Iterable[AgentEvent]:
        idx = self._round
        self._round += 1
        try:
            yield from self._rounds[idx]
        except GeneratorExit:
            self.gen_exited.append(idx)
            raise
        else:
            self.completed.append(idx)


def _total(rid: RequestId, *calls: ToolCall) -> TotalMessage:
    return TotalMessage(
        request_id=rid,
        message=AssistantMessage(tool_calls=tuple(calls)),
        finish_reason=FinishReason.STOP,
    )


def _call(call_id: str, name: str) -> ToolCall:
    return ToolCall(id=call_id, type="function", name=name, args={})


def _ctx(rid: RequestId) -> AgentContext:
    return AgentContext(request_id=rid, query="q")


def test_stop_drains_inner_to_stopiteration() -> None:
    """Стоп-круг дочитывается до StopIteration, без GeneratorExit."""
    rid = new_request_id()
    total = _total(rid)  # нет tool_calls -> StopIfReasonStop срабатывает
    src = _RecordingSource([[total]])

    events = list(AgentLoop(src, StopIfReasonStop()).stream(_ctx(rid)))

    assert src.completed == [0]
    assert src.gen_exited == []
    assert total in events


def test_drain_does_not_truncate_events_after_stop_trigger() -> None:
    """Дренаж-проброс: событие после стоп-триггера доходит до потребителя."""
    rid = new_request_id()
    total = _total(rid)  # стоп-триггер
    trailing = ToolCallMessage(request_id=rid, call=_call("x", "ghost"))
    src = _RecordingSource([[total, trailing]])

    events = list(AgentLoop(src, StopIfReasonStop()).stream(_ctx(rid)))

    assert src.completed == [0]
    assert src.gen_exited == []
    assert trailing in events


def test_continues_while_tool_calls_then_stops() -> None:
    """Есть tool_calls -> следующий круг; нет -> стоп. Каждый круг штатно закрыт."""
    rid = new_request_id()
    src = _RecordingSource(
        [
            [_total(rid, _call("a", "alpha"))],  # tool_calls -> продолжаем
            [_total(rid)],  # нет tool_calls -> стоп
        ]
    )

    events = list(AgentLoop(src, StopIfReasonStop()).stream(_ctx(rid)))

    assert src.completed == [0, 1]
    assert src.gen_exited == []
    assert sum(isinstance(e, TotalMessage) for e in events) == 2


def test_stops_on_terminal_event() -> None:
    """TerminalEvent останавливает цикл (StopOnAnyFailure), круг закрыт штатно."""
    rid = new_request_id()
    term = GenerationFailed(request_id=rid, message="boom")
    src = _RecordingSource([[term]])

    events = list(AgentLoop(src, StopOnAnyFailure()).stream(_ctx(rid)))

    assert src.completed == [0]
    assert src.gen_exited == []
    assert term in events
