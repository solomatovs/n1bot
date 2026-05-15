"""Round-trip + golden JSONL для AgentEvent + JsonLinesHistoryService.

Гарантирует:
1. Каждое из 27 финальных событий round-trip'ится через AgentEventAdapter.
2. Discriminator `type` проставляется и читается корректно.
3. Backward-compat: старые JSONL-строки (поле type первое или последнее)
   парсятся неизменно.
4. JsonLinesHistoryService end-to-end через FsHistoryWorkspaceShell.
5. InMemoryHistoryService фильтрует ContentDelta-события.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from boba.agent import (
    Advisory,
    ContentDelta,
    ContentSnapshot,
    InMemoryHistoryService,
    JsonLinesHistoryService,
    PhaseTransition,
    Terminal,
)
from boba.agent.event_specs import IsContentDelta
from boba.agent.events import (
    AgentEventAdapter,
    AnswerComplete,
    AnswerStarted,
    AnswerToken,
    FeedbackToLLMAdded,
    GenerationDone,
    GenerationFailed,
    GenerationRetried,
    GenerationStarted,
    InvalidToolCallReceived,
    IterationStarted,
    LLMRequestSent,
    LLMResponseStreamOpened,
    MaxIterationsReached,
    PersistenceFailed,
    PromptFailed,
    RefusalComplete,
    RefusalToken,
    ThinkingComplete,
    ThinkingStarted,
    ThinkingToken,
    ToolCallArgumentDelta,
    ToolCallComplete,
    ToolCallStreamStarted,
    ToolExecutionFailed,
    ToolExecutionStarted,
    ToolResultReady,
    UserQueryReceived,
)
from boba.agent.models import ToolCallFailure, ToolCallResult
from boba.agent.workspace_fs.shell import FsHistoryWorkspaceShell
from boba.llm.events import FinishReason
from boba.llm.models import InvalidToolCall, RequestId, ToolCall
from boba.tools.domain import ErrorResult, JsonResult, TextResult
from boba.workspace.contract import WorkspaceId

_RID = RequestId.from_wire("00000000-0000-0000-0000-000000000001")
_TC = ToolCall(id="call_1", name="search", args={"q": "hello"})
_ITC = InvalidToolCall(
    id="call_x",
    name="search",
    raw_args="{bad",
    error="invalid JSON",
)


def _all_events() -> list[Any]:
    return [
        # PhaseTransition (10)
        IterationStarted(request_id=_RID, iteration=1, max_iterations=5),
        LLMRequestSent(
            request_id=_RID,
            model="gpt-4",
            messages_count=3,
            has_tools=True,
            monotonic_ns=123,
        ),
        LLMResponseStreamOpened(request_id=_RID, monotonic_ns=456),
        GenerationStarted(request_id=_RID),
        ThinkingStarted(request_id=_RID),
        AnswerStarted(request_id=_RID),
        ToolCallStreamStarted(
            request_id=_RID,
            index=0,
            tool_call_id="call_1",
            tool_name="search",
        ),
        ToolExecutionStarted(request_id=_RID, call=_TC),
        GenerationRetried(
            request_id=_RID,
            attempt=2,
            reason="rate_limit",
            status_code=429,
        ),
        GenerationDone(request_id=_RID, finish_reason=FinishReason.STOP),
        # ContentDelta (4)
        ThinkingToken(request_id=_RID, token="t"),
        AnswerToken(request_id=_RID, token="a"),
        RefusalToken(request_id=_RID, token="r"),
        ToolCallArgumentDelta(
            request_id=_RID,
            index=0,
            tool_call_id="call_1",
            tool_name="search",
            arguments_chunk='{"q":',
        ),
        # ContentSnapshot (7)
        UserQueryReceived(request_id=_RID, query="hello"),
        ThinkingComplete(request_id=_RID, content="thought"),
        AnswerComplete(request_id=_RID, content="answer"),
        RefusalComplete(request_id=_RID, content="refusal"),
        ToolCallComplete(request_id=_RID, call=_TC),
        ToolResultReady(
            request_id=_RID,
            call=_TC,
            result=ToolCallResult(result=TextResult(text="ok", metadata={"k": "v"})),
        ),
        FeedbackToLLMAdded(request_id=_RID, content="critique"),
        # Advisory (2)
        InvalidToolCallReceived(request_id=_RID, invalid=_ITC),
        ToolExecutionFailed(
            request_id=_RID,
            call=_TC,
            failure=ToolCallFailure(error_kind="K", message="m"),
        ),
        # Terminal (4)
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
            iteration=10,
        ),
        PersistenceFailed(request_id=_RID, error_kind="K", message="m"),
    ]


@pytest.mark.parametrize("event", _all_events(), ids=lambda e: type(e).__name__)
def test_event_roundtrip(event: Any) -> None:
    line = AgentEventAdapter.dump_json(event).decode("utf-8")
    parsed = AgentEventAdapter.validate_json(line)
    assert parsed == event
    assert type(parsed) is type(event)
    # Discriminator type должен присутствовать и совпадать с именем класса.
    assert f'"type":"{type(event).__name__}"' in line


def test_optional_status_code_omitted() -> None:
    """status_code=None — поле сериализуется как null (а не пропускается)."""
    e = GenerationRetried(request_id=_RID, attempt=1, reason="rate_limit")
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
    assert isinstance(parsed.result.result, JsonResult)
    assert parsed.result.result.payload == {"a": [1, 2]}


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
    assert isinstance(parsed.result.result, ErrorResult)


def test_golden_jsonl_iteration_started() -> None:
    """Старый формат журнала (поле type в произвольной позиции) парсится."""
    golden = (
        '{"request_id":"00000000-0000-0000-0000-000000000001",'
        '"type":"IterationStarted","iteration":1,"max_iterations":5}'
    )
    parsed = AgentEventAdapter.validate_json(golden)
    assert isinstance(parsed, IterationStarted)
    assert parsed.iteration == 1
    assert parsed.max_iterations == 5


def test_golden_jsonl_type_first() -> None:
    golden = (
        '{"type":"IterationStarted",'
        '"request_id":"00000000-0000-0000-0000-000000000001",'
        '"iteration":1,"max_iterations":5}'
    )
    parsed = AgentEventAdapter.validate_json(golden)
    assert isinstance(parsed, IterationStarted)


def test_golden_jsonl_tool_result_ready() -> None:
    golden = (
        '{"type":"ToolResultReady",'
        '"request_id":"00000000-0000-0000-0000-000000000001",'
        '"call":{"id":"c1","name":"search","args":{"q":"x"}},'
        '"result":{"result":'
        '{"kind":"text","text":"hello","metadata":{"src":"t"}}}}'
    )
    parsed = AgentEventAdapter.validate_json(golden)
    assert isinstance(parsed, ToolResultReady)
    assert isinstance(parsed.result.result, TextResult)
    assert parsed.result.result.text == "hello"


def test_family_isinstance() -> None:
    assert isinstance(
        IterationStarted(request_id=_RID, iteration=1, max_iterations=5),
        PhaseTransition,
    )
    assert isinstance(ThinkingToken(request_id=_RID, token="t"), ContentDelta)
    assert isinstance(UserQueryReceived(request_id=_RID, query="q"), ContentSnapshot)
    assert isinstance(InvalidToolCallReceived(request_id=_RID, invalid=_ITC), Advisory)
    assert isinstance(
        GenerationFailed(request_id=_RID, error_kind="K", message="m"), Terminal
    )


def test_match_statement_dispatch() -> None:
    """Sink'и в console_sink/app.py используют match — проверяем что работает."""

    def classify(e: Any) -> str:
        match e:
            case ContentDelta():
                return "delta"
            case Terminal():
                return "terminal"
            case PhaseTransition():
                return "phase"
            case ContentSnapshot():
                return "snapshot"
            case Advisory():
                return "advisory"
            case _:
                return "?"

    assert classify(ThinkingToken(request_id=_RID, token="t")) == "delta"
    assert (
        classify(IterationStarted(request_id=_RID, iteration=1, max_iterations=1))
        == "phase"
    )
    assert classify(UserQueryReceived(request_id=_RID, query="q")) == "snapshot"
    assert (
        classify(InvalidToolCallReceived(request_id=_RID, invalid=_ITC)) == "advisory"
    )
    assert (
        classify(GenerationFailed(request_id=_RID, error_kind="K", message="m"))
        == "terminal"
    )


def test_in_memory_history_filters_content_delta() -> None:
    svc = InMemoryHistoryService()
    svc.record(IterationStarted(request_id=_RID, iteration=1, max_iterations=1))
    svc.record(ThinkingToken(request_id=_RID, token="x"))  # ContentDelta — фильтруется
    svc.record(GenerationFailed(request_id=_RID, error_kind="K", message="m"))
    recorded = list(svc.events())
    assert len(recorded) == 2
    assert isinstance(recorded[0], IterationStarted)
    assert isinstance(recorded[1], GenerationFailed)


def test_is_content_delta_spec() -> None:
    spec = IsContentDelta()
    assert spec.check(ThinkingToken(request_id=_RID, token="t"))
    assert not spec.check(
        IterationStarted(request_id=_RID, iteration=1, max_iterations=1)
    )


def test_jsonlines_history_e2e(tmp_path: Path) -> None:
    """Полный e2e: запись в файл, чтение, фильтрация ContentDelta."""
    workspace = FsHistoryWorkspaceShell(WorkspaceId.from_wire("test"), tmp_path)
    svc = JsonLinesHistoryService(workspace)

    events = [
        IterationStarted(request_id=_RID, iteration=1, max_iterations=5),
        ThinkingToken(request_id=_RID, token="x"),  # фильтруется
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
    assert len(recovered) == 3  # ThinkingToken отфильтрован
    assert recovered[0] == events[0]
    assert recovered[1] == events[2]
    assert recovered[2] == events[3]


def test_jsonlines_clear(tmp_path: Path) -> None:
    workspace = FsHistoryWorkspaceShell(WorkspaceId.from_wire("test"), tmp_path)
    svc = JsonLinesHistoryService(workspace)
    svc.record(IterationStarted(request_id=_RID, iteration=1, max_iterations=1))
    assert len(list(svc.events())) == 1
    svc.clear()
    assert len(list(svc.events())) == 0
