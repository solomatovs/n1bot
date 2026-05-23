"""ToolExecutionMiddleware: streaming-API + ToolEvent → AgentEvent конверсия.

Покрывает дуальное поведение middleware:

- **execute-стиль tool**: middleware emit'ит `ToolExecutionStarted` /
  `ToolArgsResolved` → один `ToolResultReady` (из `ToolStreamCompleted`).
  Без `ToolProgress`-событий между.

- **stream-стиль tool**: middleware emit'ит N `ToolProgress` + один
  `ToolResultReady`. Severity / details из `ToolProgressReported`
  пробрасываются в `ToolProgress`.

- **ошибка tool'а**: middleware emit'ит `ToolExecutionFailed` вместо
  `ToolResultReady`.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator

from boba.agent.agent import AgentContext
from boba.agent.events import (
    AgentEvent,
    Severity,
    ToolCallComplete,
    ToolExecutionFailed,
    ToolExecutionStarted,
    ToolProgress,
    ToolResultReady,
)
from boba.agent.middleware.tools import (
    ToolExecutionMiddleware,
    ToolToAgentConverter,
)
from boba.llm.models import ToolCall as LLMToolCall
from boba.llm.models import new_request_id
from boba.patterns import StreamSource
from boba.tools.domain import (
    JsonResult,
    TextResult,
    ToolCall,
    ToolContext,
    ToolEvent,
    ToolExecutionError,
    ToolId,
    ToolProgressReported,
    ToolSeverity,
    ToolStreamCompleted,
)


class _FakeInner(StreamSource[AgentContext, AgentEvent]):
    """Stream-source, отдающий заданный список AgentEvent'ов."""

    def __init__(self, events: list[AgentEvent]) -> None:
        self._events = events

    def name(self) -> str:
        return "FakeInner"

    def reset(self) -> None:
        pass

    def stream(self, ctx: AgentContext) -> Iterable[AgentEvent]:
        del ctx
        yield from self._events


class _FakeExecutor:
    """ToolExecutor-double: возвращает заранее заданный stream от tool'а.

    Принимает `events` либо `error`. Если `error` — `stream()` бросит
    `ToolExecutionError` после yield-а каждого event'а из `events_before_error`.
    """

    def __init__(
        self,
        events: list[ToolEvent] | None = None,
        *,
        error: ToolExecutionError | None = None,
        events_before_error: list[ToolEvent] | None = None,
    ) -> None:
        self._events = events or []
        self._error = error
        self._events_before_error = events_before_error or []
        self.calls_received: list[ToolCall] = []

    def stream(
        self,
        ctx: ToolContext,
        req: ToolCall,
    ) -> Iterator[ToolEvent]:
        del ctx
        self.calls_received.append(req)
        if self._error is not None:
            yield from self._events_before_error
            raise self._error
        yield from self._events


def _make_tc(
    request_id,
    *,
    call_id: str = "tc-1",
    name: str = "kb_ingest",
) -> ToolCallComplete:
    return ToolCallComplete(
        request_id=request_id,
        call=LLMToolCall(id=call_id, name=name, args={"x": 1}),
    )


# --------------------------------------------------------------------------- #
# ToolToAgentConverter (stateless)
# --------------------------------------------------------------------------- #


def test_converter_maps_progress_to_tool_progress():
    """`ToolProgressReported` → `ToolProgress` с corresponding-полями."""
    rid = new_request_id()
    call = LLMToolCall(id="tc-1", name="kb_ingest", args={})

    events = list(
        ToolToAgentConverter().convert(
            ToolProgressReported(
                headline="indexed 12/100 pages",
                details={"space": "DOCS", "page_id": "950276"},
                severity=ToolSeverity.WARN,
            ),
            request_id=rid,
            call=call,
        ),
    )

    assert len(events) == 1
    ev = events[0]
    assert isinstance(ev, ToolProgress)
    assert ev.tool_call_id == "tc-1"
    assert ev.tool_name == "kb_ingest"
    assert ev.headline == "indexed 12/100 pages"
    assert ev.severity == Severity.WARN
    assert ev.details == {"space": "DOCS", "page_id": "950276"}
    # label дериватся из tool_name + headline
    assert "kb_ingest" in ev.label
    assert "indexed 12/100 pages" in ev.label


def test_converter_maps_completed_to_tool_result_ready():
    """`ToolStreamCompleted` → `ToolResultReady` с result обёрнутым в ToolCallResult."""
    rid = new_request_id()
    call = LLMToolCall(id="tc-1", name="kb_ingest", args={})

    events = list(
        ToolToAgentConverter().convert(
            ToolStreamCompleted(result=TextResult(text="done")),
            request_id=rid,
            call=call,
        ),
    )

    assert len(events) == 1
    ev = events[0]
    assert isinstance(ev, ToolResultReady)
    assert ev.call is call
    assert ev.result.result == TextResult(text="done")


# --------------------------------------------------------------------------- #
# ToolExecutionMiddleware: execute-style tool
# --------------------------------------------------------------------------- #


def test_middleware_execute_style_no_progress_events(agent_ctx: AgentContext):
    """Tool, эмитящий один `ToolStreamCompleted` — без `ToolProgress`."""
    tc = _make_tc(agent_ctx.request_id)
    inner = _FakeInner([tc])
    fake_executor = _FakeExecutor([
        ToolStreamCompleted(result=TextResult(text="done")),
    ])
    mw = ToolExecutionMiddleware(inner, fake_executor)  # type: ignore[arg-type]

    out = list(mw.stream(agent_ctx))
    types = [type(e).__name__ for e in out]

    # inner event (ToolCallComplete) проходит насквозь, дальше — наша троица.
    assert types == [
        "ToolCallComplete",
        "ToolExecutionStarted",
        "ToolArgsResolved",
        "ToolResultReady",
    ]
    result_ready = out[-1]
    assert isinstance(result_ready, ToolResultReady)
    assert result_ready.result.result == TextResult(text="done")


# --------------------------------------------------------------------------- #
# ToolExecutionMiddleware: stream-style tool
# --------------------------------------------------------------------------- #


def test_middleware_stream_style_emits_progress_and_result(agent_ctx: AgentContext):
    """N progress + final → N ToolProgress + один ToolResultReady."""
    tc = _make_tc(agent_ctx.request_id)
    inner = _FakeInner([tc])
    fake_executor = _FakeExecutor([
        ToolProgressReported(headline="step 1/3"),
        ToolProgressReported(headline="step 2/3"),
        ToolProgressReported(
            headline="step 3/3",
            details={"final": "yes"},
            severity=ToolSeverity.WARN,
        ),
        ToolStreamCompleted(result=JsonResult(payload={"total": 3})),
    ])
    mw = ToolExecutionMiddleware(inner, fake_executor)  # type: ignore[arg-type]

    out = list(mw.stream(agent_ctx))
    types = [type(e).__name__ for e in out]

    assert types == [
        "ToolCallComplete",
        "ToolExecutionStarted",
        "ToolArgsResolved",
        "ToolProgress",
        "ToolProgress",
        "ToolProgress",
        "ToolResultReady",
    ]
    progress_events = [e for e in out if isinstance(e, ToolProgress)]
    assert [e.headline for e in progress_events] == ["step 1/3", "step 2/3", "step 3/3"]
    assert progress_events[-1].severity == Severity.WARN
    assert progress_events[-1].details == {"final": "yes"}
    # tool_call_id/tool_name связывают прогресс со ToolExecutionStarted
    started = next(e for e in out if isinstance(e, ToolExecutionStarted))
    for pe in progress_events:
        assert pe.tool_call_id == started.tool_call_id
        assert pe.tool_name == started.tool_name


def test_middleware_stream_dispatch_call_correct(agent_ctx: AgentContext):
    """Middleware пробрасывает корректный ToolCall в executor (tool_id + args)."""
    tc = _make_tc(agent_ctx.request_id, call_id="abc", name="my_tool")
    fake_executor = _FakeExecutor([
        ToolStreamCompleted(result=TextResult(text="x")),
    ])
    mw = ToolExecutionMiddleware(
        _FakeInner([tc]),
        fake_executor,  # type: ignore[arg-type]
    )

    list(mw.stream(agent_ctx))

    assert len(fake_executor.calls_received) == 1
    received = fake_executor.calls_received[0]
    assert received.tool_id == ToolId("my_tool")
    assert received.arguments == {"x": 1}


# --------------------------------------------------------------------------- #
# ToolExecutionMiddleware: error path
# --------------------------------------------------------------------------- #


def test_middleware_tool_error_emits_execution_failed(agent_ctx: AgentContext):
    """ToolExecutionError от executor → ToolExecutionFailed (без ToolResultReady)."""
    tc = _make_tc(agent_ctx.request_id)
    fake_executor = _FakeExecutor(
        error=ToolExecutionError(ToolId("kb_ingest"), "boom"),
    )
    mw = ToolExecutionMiddleware(
        _FakeInner([tc]),
        fake_executor,  # type: ignore[arg-type]
    )

    out = list(mw.stream(agent_ctx))
    types = [type(e).__name__ for e in out]

    assert types == [
        "ToolCallComplete",
        "ToolExecutionStarted",
        "ToolArgsResolved",
        "ToolExecutionFailed",
    ]
    failed = out[-1]
    assert isinstance(failed, ToolExecutionFailed)
    assert "boom" in failed.failure.message


def test_middleware_progress_before_error_still_yielded(agent_ctx: AgentContext):
    """Progress-события, эмитнутые до raise, не теряются."""
    tc = _make_tc(agent_ctx.request_id)
    fake_executor = _FakeExecutor(
        events_before_error=[ToolProgressReported(headline="step 1")],
        error=ToolExecutionError(ToolId("kb_ingest"), "boom"),
    )
    mw = ToolExecutionMiddleware(
        _FakeInner([tc]),
        fake_executor,  # type: ignore[arg-type]
    )

    out = list(mw.stream(agent_ctx))
    types = [type(e).__name__ for e in out]

    assert types == [
        "ToolCallComplete",
        "ToolExecutionStarted",
        "ToolArgsResolved",
        "ToolProgress",
        "ToolExecutionFailed",
    ]
