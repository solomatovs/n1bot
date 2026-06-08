"""Round-trip + golden JSONL для AgentEvent + JsonLinesHistoryService.

Гарантирует:
1. Каждое из 19 финальных событий round-trip'ится через AgentEventAdapter.
2. Discriminator type проставляется и читается корректно.
3. Backward-compat: старые JSONL-строки (поле type первое или последнее)
   парсятся неизменно.
4. JsonLinesHistoryService end-to-end через FsWorkspaceShell.
5. InMemoryHistoryService фильтрует DeltaEvent и DiagnosticEvent.
"""

from __future__ import annotations

from typing import Any, cast
from uuid import UUID

import pytest

from boba.agent import (
    AdvisoryEvent,
    DeltaEvent,
    DiagnosticEvent,
    InMemoryHistoryService,
    JsonLinesHistoryService,
    MessageEvent,
    PhaseEvent,
    TerminalEvent,
)
from boba.agent.event_specs import IsContentDelta
from boba.agent.events import (
    AgentEvent,
    AgentEventAdapter,
    AnswerDelta,
    AnswerMessage,
    FeedbackToLLMAdded,
    GenerationFailed,
    IterationStarted,
    MaxIterationsReached,
    PersistenceFailed,
    PromptFailed,
    RefusalDelta,
    RefusalMessage,
    ThinkingDelta,
    ThinkingMessage,
    ToolCallDecodeFailedMessage,
    ToolCallDelta,
    ToolCallMessage,
    ToolExecutionFailed,
    ToolExecutionStarted,
    ToolResultReady,
    UserQueryReceived,
)
from boba.agent.models import ToolCallFailure, ToolCallResult
from boba.agent.workspace_fs.shell import FsWorkspaceShell
from boba.llm.models import RequestId, ToolCall, ToolCallDecodeFailure
from boba.tools.domain import ChartResult, ErrorResult, JsonResult, TextResult

_RID = RequestId(UUID("00000000-0000-0000-0000-000000000001"))
_TC = ToolCall(id="call_1", type="function", name="search", args={"q": "hello"})
_TCDF = ToolCallDecodeFailure(
    id="call_x",
    type="function",
    name="search",
    raw="{bad",
    error="invalid JSON",
)


def _all_events() -> list[Any]:
    return [
        # PhaseEvent (2)
        IterationStarted(request_id=_RID, iteration_count=1, max_iterations=5),
        ToolExecutionStarted(
            request_id=_RID,
            call=_TC,
        ),
        # DeltaEvent (4)
        ThinkingDelta(request_id=_RID, token="t"),
        AnswerDelta(request_id=_RID, token="a"),
        RefusalDelta(request_id=_RID, token="r"),
        ToolCallDelta(
            request_id=_RID,
            index=0,
            tool_call_id="call_1",
            tool_name="search",
            arguments_chunk='{"q":',
        ),
        # MessageEvent (8)
        UserQueryReceived(request_id=_RID, query="hello"),
        ThinkingMessage(request_id=_RID, content="thought"),
        AnswerMessage(request_id=_RID, content="answer"),
        RefusalMessage(request_id=_RID, content="refusal"),
        ToolCallMessage(request_id=_RID, call=_TC),
        ToolCallDecodeFailedMessage(request_id=_RID, failure=_TCDF),
        ToolResultReady(
            request_id=_RID,
            call=_TC,
            result=ToolCallResult(result=TextResult(text="ok", metadata={"k": "v"})),
        ),
        FeedbackToLLMAdded(request_id=_RID, content="critique"),
        # AdvisoryEvent (1)
        ToolExecutionFailed(
            request_id=_RID,
            call=_TC,
            failure=ToolCallFailure(error_kind="K", message="m"),
        ),
        # TerminalEvent (4)
        GenerationFailed(request_id=_RID, error_kind="K", message="m"),
        PromptFailed(
            request_id=_RID,
            error_kind="K",
            message="m",
            provider="openai",
        ),
        MaxIterationsReached(
            request_id=_RID,
            error_kind="K",
            message="m",
            limit=10,
            iteration_count=10,
        ),
        PersistenceFailed(request_id=_RID, error_kind="K", message="m"),
    ]


@pytest.mark.parametrize("event", _all_events(), ids=lambda e: type(e).__name__)
def test_event_roundtrip(event: Any) -> None:
    line = AgentEventAdapter.dump_json(event).decode("utf-8")
    parsed = AgentEventAdapter.validate_json(line)
    assert parsed == event
    assert type(parsed) is type(event)
    # Discriminator type должен присутствовать и совпадать с Literal-полем.
    assert f'"type":"{event.type}"' in line


def test_optional_status_code_omitted() -> None:
    """status_code=None — поле сериализуется как null (а не пропускается)."""
    e = ToolExecutionFailed(
        request_id=_RID,
        call=_TC,
        failure=ToolCallFailure(error_kind="K", message="m"),
    )
    line = AgentEventAdapter.dump_json(e).decode("utf-8")
    assert '"status_code":null' in line
    parsed = AgentEventAdapter.validate_json(line)
    assert parsed == e


def test_optional_provider_omitted() -> None:
    e = PromptFailed(request_id=_RID, error_kind="K", message="m")
    line = AgentEventAdapter.dump_json(e).decode("utf-8")
    assert '"provider":null' in line
    parsed = AgentEventAdapter.validate_json(line)
    assert parsed == e


def test_tool_result_text_variant() -> None:
    e = ToolResultReady(
        request_id=_RID,
        call=_TC,
        result=ToolCallResult(result=TextResult(text="x", metadata={"a": "b"})),
    )
    line = AgentEventAdapter.dump_json(e).decode("utf-8")
    assert '"kind":"text"' in line
    parsed = AgentEventAdapter.validate_json(line)
    assert isinstance(parsed, ToolResultReady)
    assert isinstance(parsed.result.result, TextResult)


def test_tool_result_json_variant() -> None:
    e = ToolResultReady(
        request_id=_RID,
        call=_TC,
        result=ToolCallResult(result=JsonResult(payload={"a": [1, 2]}, metadata={})),
    )
    line = AgentEventAdapter.dump_json(e).decode("utf-8")
    assert '"kind":"json"' in line
    parsed = AgentEventAdapter.validate_json(line)
    assert isinstance(parsed, ToolResultReady)
    assert isinstance(parsed.result.result, JsonResult)
    assert parsed.result.result.payload == {"a": [1, 2]}


def test_tool_result_chart_variant() -> None:
    e = ToolResultReady(
        request_id=_RID,
        call=_TC,
        result=ToolCallResult(
            result=ChartResult(
                spec={"data": [{"type": "bar", "x": ["a"], "y": [1]}], "layout": {}},
                title="Продажи",
            ),
        ),
    )
    line = AgentEventAdapter.dump_json(e).decode("utf-8")
    assert '"kind":"chart"' in line
    parsed = AgentEventAdapter.validate_json(line)
    assert isinstance(parsed, ToolResultReady)
    assert isinstance(parsed.result.result, ChartResult)
    assert parsed.result.result.title == "Продажи"
    assert parsed.result.result.spec["data"][0]["type"] == "bar"
    # В историю/LLM проецируется сводка, а не сырой spec.
    assert parsed.body == "[chart rendered: Продажи]"


def test_tool_result_error_variant() -> None:
    e = ToolResultReady(
        request_id=_RID,
        call=_TC,
        result=ToolCallResult(
            result=ErrorResult(message="boom", error_kind="DomainError", metadata={}),
        ),
    )
    line = AgentEventAdapter.dump_json(e).decode("utf-8")
    assert '"kind":"error"' in line
    parsed = AgentEventAdapter.validate_json(line)
    assert isinstance(parsed, ToolResultReady)
    assert isinstance(parsed.result.result, ErrorResult)


def test_golden_jsonl_iteration_started() -> None:
    """Старый формат журнала (поле type в произвольной позиции) парсится."""
    golden = (
        '{"request_id":"00000000-0000-0000-0000-000000000001",'
        '"type":"IterationStarted","iteration_count":1,"max_iterations":5}'
    )
    parsed = AgentEventAdapter.validate_json(golden)
    assert isinstance(parsed, IterationStarted)
    assert parsed.iteration_count == 1
    assert parsed.max_iterations == 5


def test_golden_jsonl_type_first() -> None:
    golden = (
        '{"type":"IterationStarted",'
        '"request_id":"00000000-0000-0000-0000-000000000001",'
        '"iteration_count":1,"max_iterations":5}'
    )
    parsed = AgentEventAdapter.validate_json(golden)
    assert isinstance(parsed, IterationStarted)


def test_golden_jsonl_tool_result_ready() -> None:
    golden = (
        '{"type":"ToolResultReady",'
        '"request_id":"00000000-0000-0000-0000-000000000001",'
        '"call":{"id":"c1","type":"function","name":"search","args":{"q":"x"}},'
        '"result":{"result":'
        '{"kind":"text","text":"hello","metadata":{"src":"t"}}}}'
    )
    parsed = AgentEventAdapter.validate_json(golden)
    assert isinstance(parsed, ToolResultReady)
    assert isinstance(parsed.result.result, TextResult)
    assert parsed.result.result.text == "hello"


def test_family_isinstance() -> None:
    assert isinstance(
        IterationStarted(request_id=_RID, iteration_count=1, max_iterations=5),
        PhaseEvent,
    )
    assert isinstance(ThinkingDelta(request_id=_RID, token="t"), DeltaEvent)
    assert isinstance(
        UserQueryReceived(request_id=_RID, query="q"),
        MessageEvent,
    )
    assert isinstance(
        ToolCallDecodeFailedMessage(request_id=_RID, failure=_TCDF),
        MessageEvent,
    )
    assert isinstance(
        ToolExecutionFailed(
            request_id=_RID,
            call=_TC,
            failure=ToolCallFailure(error_kind="K", message="m"),
        ),
        AdvisoryEvent,
    )
    assert isinstance(
        GenerationFailed(request_id=_RID, error_kind="K", message="m"),
        TerminalEvent,
    )


def test_match_statement_dispatch() -> None:
    """Sink'и в console_sink/app.py используют match — проверяем что работает."""

    def classify(e: Any) -> str:
        match e:
            case DeltaEvent():
                return "delta"
            case TerminalEvent():
                return "terminal"
            case PhaseEvent():
                return "phase"
            case MessageEvent():
                return "snapshot"
            case AdvisoryEvent():
                return "advisory"
            case _:
                return "?"

    assert classify(ThinkingDelta(request_id=_RID, token="t")) == "delta"
    assert (
        classify(
            IterationStarted(request_id=_RID, iteration_count=1, max_iterations=1),
        )
        == "phase"
    )
    assert classify(UserQueryReceived(request_id=_RID, query="q")) == "snapshot"
    assert (
        classify(ToolCallDecodeFailedMessage(request_id=_RID, failure=_TCDF))
        == "snapshot"
    )
    assert (
        classify(
            ToolExecutionFailed(
                request_id=_RID,
                call=_TC,
                failure=ToolCallFailure(error_kind="K", message="m"),
            ),
        )
        == "advisory"
    )
    assert (
        classify(GenerationFailed(request_id=_RID, error_kind="K", message="m"))
        == "terminal"
    )


def test_in_memory_history_filters_content_delta_and_diagnostic() -> None:
    svc = InMemoryHistoryService()
    svc.record(
        IterationStarted(request_id=_RID, iteration_count=1, max_iterations=1),
    )
    # DeltaEvent — фильтруется
    svc.record(ThinkingDelta(request_id=_RID, token="x"))
    # DiagnosticEvent — фильтруется (эфемерная телеметрия). Конкретных core-
    # диагностик больше нет, поэтому фильтрацию по категории проверяем базовым
    # DiagnosticEvent — он намеренно вне sealed-union AgentEvent.
    diagnostic = DiagnosticEvent(request_id=_RID, type="TestDiagnostic")
    svc.record(cast(AgentEvent, diagnostic))
    svc.record(GenerationFailed(request_id=_RID, error_kind="K", message="m"))
    recorded = list(svc.events())
    assert len(recorded) == 2
    assert isinstance(recorded[0], IterationStarted)
    assert isinstance(recorded[1], GenerationFailed)


def test_diagnostic_event_is_diagnostic_family() -> None:
    e = DiagnosticEvent(request_id=_RID, type="TestDiagnostic", topic="t")
    assert isinstance(e, DiagnosticEvent)
    assert e.category.value == "diagnostic"


def test_is_content_delta_spec() -> None:
    spec = IsContentDelta()
    assert spec.check(ThinkingDelta(request_id=_RID, token="t"))
    assert not spec.check(
        IterationStarted(request_id=_RID, iteration_count=1, max_iterations=1),
    )


def test_jsonlines_history_e2e(history_workspace: FsWorkspaceShell) -> None:
    """Полный e2e: запись в файл, чтение, фильтрация DeltaEvent."""
    svc = JsonLinesHistoryService(history_workspace)

    events = [
        IterationStarted(request_id=_RID, iteration_count=1, max_iterations=5),
        ThinkingDelta(request_id=_RID, token="x"),  # фильтруется
        ToolResultReady(
            request_id=_RID,
            call=_TC,
            result=ToolCallResult(result=TextResult(text="ok", metadata={})),
        ),
        GenerationFailed(request_id=_RID, error_kind="K", message="m"),
    ]
    for e in events:
        svc.record(e)

    recovered = list(svc.events())
    assert len(recovered) == 3  # ThinkingDelta отфильтрован
    assert recovered[0] == events[0]
    assert recovered[1] == events[2]
    assert recovered[2] == events[3]


def test_jsonlines_clear(history_workspace: FsWorkspaceShell) -> None:
    svc = JsonLinesHistoryService(history_workspace)
    svc.record(
        IterationStarted(request_id=_RID, iteration_count=1, max_iterations=1),
    )
    assert len(list(svc.events())) == 1
    svc.clear()
    assert len(list(svc.events())) == 0
