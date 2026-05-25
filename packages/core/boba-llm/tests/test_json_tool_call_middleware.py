"""Юнит-тесты JsonContentToolCallMiddleware."""

from __future__ import annotations

import json
from collections.abc import Iterable

from boba.llm.events import (
    FinishReason,
    LLMAnswerComplete,
    LLMAnswerStarted,
    LLMAnswerToken,
    LLMEvent,
    LLMGenerationDone,
    LLMGenerationResult,
    LLMGenerationStarted,
    LLMThinkingStarted,
    LLMThinkingToken,
    LLMToolCallArgumentDelta,
    LLMToolCallBegin,
    LLMToolCallComplete,
)
from boba.llm.middleware import AssistantAggregator, JsonContentToolCallMiddleware
from boba.llm.models import LLMContext, LLMRequest, RequestId, new_request_id
from boba.patterns import StreamSource


class _ScriptedSource(StreamSource[LLMContext, LLMEvent]):
    """Источник, который проигрывает заранее заданный список событий."""

    def __init__(self, events: list[LLMEvent]) -> None:
        self._events = list(events)

    def name(self) -> str:
        return "Scripted"

    def reset(self) -> None:
        pass

    def stream(self, ctx: LLMContext) -> Iterable[LLMEvent]:
        yield from self._events


def _ctx(rid: RequestId) -> LLMContext:
    return LLMContext(
        request=LLMRequest(request_id=rid, model="test-model"),
    )


def _run(events: list[LLMEvent], rid: RequestId) -> list[LLMEvent]:
    inner = _ScriptedSource(events)
    mw = JsonContentToolCallMiddleware(inner)
    return list(mw.stream(_ctx(rid)))


def test_json_object_becomes_tool_call_deltas() -> None:
    rid = new_request_id()
    events: list[LLMEvent] = [
        LLMGenerationStarted(request_id=rid),
        LLMAnswerStarted(request_id=rid),
        LLMAnswerToken(request_id=rid, token='{"name"'),
        LLMAnswerToken(request_id=rid, token=': "shell_run", '),
        LLMAnswerToken(request_id=rid, token='"arguments"'),
        LLMAnswerToken(request_id=rid, token=': {"cmd": "ls"}}'),
        LLMGenerationDone(request_id=rid, finish_reason=FinishReason.STOP),
    ]

    out = _run(events, rid)

    assert isinstance(out[0], LLMGenerationStarted)
    assert not any(isinstance(e, LLMAnswerStarted | LLMAnswerToken) for e in out)

    begins = [e for e in out if isinstance(e, LLMToolCallBegin)]
    deltas = [e for e in out if isinstance(e, LLMToolCallArgumentDelta)]
    dones = [e for e in out if isinstance(e, LLMGenerationDone)]

    assert len(begins) == 1
    assert begins[0].tool_name == "shell_run"
    assert begins[0].index == 0
    assert begins[0].tool_call_id.startswith("call_")

    assert len(deltas) == 1
    assert json.loads(deltas[0].arguments) == {"cmd": "ls"}
    assert deltas[0].tool_call_id == begins[0].tool_call_id

    assert len(dones) == 1
    assert dones[0].finish_reason is FinishReason.TOOL_CALLS


def test_plain_text_passes_through_unchanged() -> None:
    rid = new_request_id()
    events: list[LLMEvent] = [
        LLMGenerationStarted(request_id=rid),
        LLMAnswerStarted(request_id=rid),
        LLMAnswerToken(request_id=rid, token="hello "),
        LLMAnswerToken(request_id=rid, token="world"),
        LLMGenerationDone(request_id=rid, finish_reason=FinishReason.STOP),
    ]

    out = _run(events, rid)

    assert out == events


def test_invalid_json_rolls_back_to_text() -> None:
    rid = new_request_id()
    events: list[LLMEvent] = [
        LLMGenerationStarted(request_id=rid),
        LLMAnswerStarted(request_id=rid),
        LLMAnswerToken(request_id=rid, token='{"name": "x", '),
        LLMAnswerToken(request_id=rid, token='"arguments": <broken>'),
        LLMGenerationDone(request_id=rid, finish_reason=FinishReason.STOP),
    ]

    out = _run(events, rid)

    assert isinstance(out[0], LLMGenerationStarted)
    assert isinstance(out[1], LLMAnswerStarted)
    assert isinstance(out[2], LLMAnswerToken)
    assert out[2].token == '{"name": "x", "arguments": <broken>'
    assert isinstance(out[3], LLMGenerationDone)
    assert out[3].finish_reason is FinishReason.STOP

    assert not any(isinstance(e, LLMToolCallBegin) for e in out)


def test_json_without_name_arguments_shape_rolls_back() -> None:
    rid = new_request_id()
    raw = '{"foo": "bar"}'
    events: list[LLMEvent] = [
        LLMGenerationStarted(request_id=rid),
        LLMAnswerStarted(request_id=rid),
        LLMAnswerToken(request_id=rid, token=raw),
        LLMGenerationDone(request_id=rid, finish_reason=FinishReason.STOP),
    ]

    out = _run(events, rid)

    assert any(isinstance(e, LLMAnswerStarted) for e in out)
    tokens = [e for e in out if isinstance(e, LLMAnswerToken)]
    assert len(tokens) == 1
    assert tokens[0].token == raw
    assert not any(isinstance(e, LLMToolCallBegin) for e in out)


def test_leading_whitespace_before_brace_still_buffered() -> None:
    rid = new_request_id()
    events: list[LLMEvent] = [
        LLMGenerationStarted(request_id=rid),
        LLMAnswerStarted(request_id=rid),
        LLMAnswerToken(request_id=rid, token='  \n{"name": "t", "arguments": {}}'),
        LLMGenerationDone(request_id=rid, finish_reason=FinishReason.STOP),
    ]

    out = _run(events, rid)

    assert any(
        isinstance(e, LLMToolCallBegin) and e.tool_name == "t" for e in out
    )
    assert not any(isinstance(e, LLMAnswerToken) for e in out)


def test_first_token_not_json_passes_remaining_through() -> None:
    rid = new_request_id()
    events: list[LLMEvent] = [
        LLMGenerationStarted(request_id=rid),
        LLMAnswerStarted(request_id=rid),
        LLMAnswerToken(request_id=rid, token="hi"),
        LLMAnswerToken(request_id=rid, token=" {looks-like-json}"),
        LLMGenerationDone(request_id=rid, finish_reason=FinishReason.STOP),
    ]

    out = _run(events, rid)

    tokens = [e for e in out if isinstance(e, LLMAnswerToken)]
    assert [t.token for t in tokens] == ["hi", " {looks-like-json}"]


def test_finely_split_stream_with_thinking_like_qwen_via_litellm() -> None:
    """Воспроизводит реальный сплит ollama/qwen через litellm: thinking → JSON по символам."""
    rid = new_request_id()
    json_pieces = [
        "{",
        '\n "',
        "name",
        '":',
        ' "shell_run", "arguments": ',
        '{"command": "ls"}}',
    ]
    events: list[LLMEvent] = [
        LLMGenerationStarted(request_id=rid),
        LLMThinkingStarted(request_id=rid),
        LLMThinkingToken(request_id=rid, token="The"),
        LLMThinkingToken(request_id=rid, token=" user"),
        LLMAnswerStarted(request_id=rid),
        *(LLMAnswerToken(request_id=rid, token=p) for p in json_pieces),
        LLMGenerationDone(request_id=rid, finish_reason=FinishReason.STOP),
    ]

    inner = _ScriptedSource(events)
    chain = AssistantAggregator(JsonContentToolCallMiddleware(inner))
    out = list(chain.stream(_ctx(rid)))

    # thinking сохраняется как есть
    assert any(isinstance(e, LLMThinkingStarted) for e in out)
    thinking_tokens = [e for e in out if isinstance(e, LLMThinkingToken)]
    assert [t.token for t in thinking_tokens] == ["The", " user"]

    # answer-токенов наружу не уехало (всё ушло в tool-call)
    assert not any(isinstance(e, LLMAnswerToken | LLMAnswerStarted) for e in out)
    assert not any(isinstance(e, LLMAnswerComplete) for e in out)

    # на выходе агрегатора — корректный tool-call
    results = [e for e in out if isinstance(e, LLMGenerationResult)]
    assert len(results) == 1
    assert results[0].finish_reason is FinishReason.TOOL_CALLS
    assert len(results[0].message.tool_calls) == 1
    assert results[0].message.tool_calls[0].name == "shell_run"
    assert results[0].message.tool_calls[0].args == {"command": "ls"}
    assert results[0].message.thinking == "The user"


def test_aggregator_above_collects_synthetic_tool_call() -> None:
    """Sanity: middleware + AssistantAggregator выдают корректный AssistantMessage."""
    rid = new_request_id()
    events: list[LLMEvent] = [
        LLMGenerationStarted(request_id=rid),
        LLMAnswerStarted(request_id=rid),
        LLMAnswerToken(
            request_id=rid,
            token='{"name": "fs_read", "arguments": {"path": "/etc/hosts"}}',
        ),
        LLMGenerationDone(request_id=rid, finish_reason=FinishReason.STOP),
    ]

    inner = _ScriptedSource(events)
    chain = AssistantAggregator(JsonContentToolCallMiddleware(inner))
    out = list(chain.stream(_ctx(rid)))

    completes = [e for e in out if isinstance(e, LLMToolCallComplete)]
    results = [e for e in out if isinstance(e, LLMGenerationResult)]
    assert not any(isinstance(e, LLMAnswerComplete) for e in out)

    assert len(completes) == 1
    assert completes[0].call.name == "fs_read"
    assert completes[0].call.args == {"path": "/etc/hosts"}

    assert len(results) == 1
    assert results[0].finish_reason is FinishReason.TOOL_CALLS
    assert len(results[0].message.tool_calls) == 1
    assert results[0].message.tool_calls[0].name == "fs_read"
    assert results[0].message.content == ""
