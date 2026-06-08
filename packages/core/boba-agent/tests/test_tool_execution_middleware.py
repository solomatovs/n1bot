"""Контракт ToolExecutionMiddleware: исполнение из итогового TotalMessage.

Источник истины исполнения — TotalMessage.message.tool_calls. Потоковые
ToolCallMessage наблюдательные и на исполнение не влияют.
"""

from __future__ import annotations

from collections.abc import Iterable

from boba.agent.agent import AgentContext
from boba.agent.events import (
    AgentEvent,
    ToolCallMessage,
    ToolExecutionFailed,
    ToolExecutionStarted,
    ToolResultReady,
    TotalMessage,
)
from boba.agent.middleware.tools import ToolExecutionMiddleware
from boba.llm.events import FinishReason
from boba.llm.models import (
    AssistantMessage,
    RequestId,
    ToolCall,
    new_request_id,
)
from boba.patterns import StreamSource
from boba.tools.domain import (
    TextResult,
    ToolContext,
    ToolExecutionError,
)
from boba.tools.domain import ToolCall as DomainToolCall


class _ScriptedInner(StreamSource[AgentContext, AgentEvent]):
    """Inner-стрим, эмитящий заранее заданный список событий."""

    def __init__(self, events: list[AgentEvent]) -> None:
        self._events = events

    def name(self) -> str:
        return "ScriptedInner"

    def reset(self) -> None:
        pass

    def stream(self, ctx: AgentContext) -> Iterable[AgentEvent]:
        yield from self._events


class _RecordingExecutor:
    """Fake ToolExecutor: пишет исполненные вызовы; падает на именах из fail."""

    def __init__(self, fail: set[str] | None = None) -> None:
        self.executed: list[DomainToolCall] = []
        self._fail = fail or set()

    def execute(self, ctx: ToolContext, req: DomainToolCall) -> TextResult:
        self.executed.append(req)
        if str(req.tool_id) in self._fail:
            raise ToolExecutionError(tool_id=req.tool_id, message="boom")
        return TextResult(text=f"ok:{req.tool_id}")


def _total(rid: RequestId, *calls: ToolCall) -> TotalMessage:
    return TotalMessage(
        request_id=rid,
        message=AssistantMessage(tool_calls=tuple(calls)),
        finish_reason=FinishReason.STOP,
    )


def _call(call_id: str, name: str, **args: object) -> ToolCall:
    return ToolCall(id=call_id, type="function", name=name, args=args)


def _ctx(rid: RequestId) -> AgentContext:
    return AgentContext(request_id=rid, query="q")


def test_executes_tool_calls_from_total_message():
    """Вызовы из TotalMessage.message.tool_calls исполняются по порядку."""
    rid = new_request_id()
    executor = _RecordingExecutor()
    inner = _ScriptedInner([_total(rid, _call("a", "alpha"), _call("b", "beta"))])
    mw = ToolExecutionMiddleware(inner, executor)  # type: ignore[arg-type]

    events = list(mw.stream(_ctx(rid)))

    assert [str(c.tool_id) for c in executor.executed] == ["alpha", "beta"]
    started = [e for e in events if isinstance(e, ToolExecutionStarted)]
    results = [e for e in events if isinstance(e, ToolResultReady)]
    assert [e.call.id for e in started] == ["a", "b"]
    assert [e.call.id for e in results] == ["a", "b"]


def test_tool_call_message_is_observational_only():
    """ToolCallMessage в потоке наблюдательный — НЕ приводит к исполнению."""
    rid = new_request_id()
    executor = _RecordingExecutor()
    observational = ToolCallMessage(request_id=rid, call=_call("x", "ghost"))
    inner = _ScriptedInner([observational, _total(rid)])
    mw = ToolExecutionMiddleware(inner, executor)  # type: ignore[arg-type]

    events = list(mw.stream(_ctx(rid)))

    assert executor.executed == []
    assert not any(isinstance(e, ToolExecutionStarted) for e in events)
    # наблюдательное событие проходит насквозь
    assert observational in events


def test_execution_error_becomes_failed_event():
    """ToolExecutionError превращается в ToolExecutionFailed, не прерывая остальные."""
    rid = new_request_id()
    executor = _RecordingExecutor(fail={"alpha"})
    inner = _ScriptedInner([_total(rid, _call("a", "alpha"), _call("b", "beta"))])
    mw = ToolExecutionMiddleware(inner, executor)  # type: ignore[arg-type]

    events = list(mw.stream(_ctx(rid)))

    failed = [e for e in events if isinstance(e, ToolExecutionFailed)]
    results = [e for e in events if isinstance(e, ToolResultReady)]
    assert [e.call.id for e in failed] == ["a"]
    assert [e.call.id for e in results] == ["b"]


def test_empty_tool_calls_no_execution():
    """TotalMessage без tool_calls не порождает исполнения."""
    rid = new_request_id()
    executor = _RecordingExecutor()
    inner = _ScriptedInner([_total(rid)])
    mw = ToolExecutionMiddleware(inner, executor)  # type: ignore[arg-type]

    events = list(mw.stream(_ctx(rid)))

    assert executor.executed == []
    assert not any(
        isinstance(e, (ToolExecutionStarted, ToolResultReady, ToolExecutionFailed))
        for e in events
    )
