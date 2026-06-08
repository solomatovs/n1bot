"""Тесты консьюмеров ответа OpenAI: stream (chunks) и non-stream (ChatCompletion)."""

from __future__ import annotations

from typing import Any

from openai.types.chat import ChatCompletion
from openai.types.chat.chat_completion_chunk import ChatCompletionChunk

from boba.llm.events import (
    FinishReason,
    LLMAnswerDelta,
    LLMAnswerMessage,
    LLMThinkingDelta,
    LLMThinkingMessage,
    LLMToolCallDelta,
    LLMToolCallMessage,
    LLMTotalMessage,
)
from boba.llm.models import LLMContext, LLMRequest, new_request_id
from boba.provider.openai.response import (
    ChatCompletionChunkConsumer,
    ChatCompletionConsumer,
    ToolCallFromContentFallback,
)


def _completion(message: dict[str, Any], finish_reason: str = "stop") -> ChatCompletion:
    return ChatCompletion.model_validate(
        {
            "id": "cmpl-1",
            "created": 0,
            "model": "test-model",
            "object": "chat.completion",
            "choices": [
                {"index": 0, "finish_reason": finish_reason, "message": message},
            ],
        }
    )


def _chunk(
    delta: dict[str, Any], finish_reason: str | None = None
) -> ChatCompletionChunk:
    return ChatCompletionChunk.model_validate(
        {
            "id": "chunk-1",
            "created": 0,
            "model": "test-model",
            "object": "chat.completion.chunk",
            "choices": [
                {"index": 0, "delta": delta, "finish_reason": finish_reason},
            ],
        }
    )


# --- non-stream ----------------------------------------------------------- #


def test_non_stream_emits_only_snapshots() -> None:
    rid = new_request_id()
    response = _completion({"role": "assistant", "content": "hello"})

    events = list(ChatCompletionConsumer(rid).consume(response))

    # никаких delta-событий — только итоговые
    assert not any(isinstance(e, LLMAnswerDelta) for e in events)
    assert [type(e) for e in events] == [LLMAnswerMessage, LLMTotalMessage]

    answer, result = events
    assert isinstance(answer, LLMAnswerMessage)
    assert answer.content == "hello"
    assert isinstance(result, LLMTotalMessage)
    assert result.finish_reason is FinishReason.STOP
    assert result.message.content == "hello"


def test_non_stream_tool_call_parsed() -> None:
    rid = new_request_id()
    response = _completion(
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "search", "arguments": '{"q": "x"}'},
                },
            ],
        },
        finish_reason="tool_calls",
    )

    events = list(ChatCompletionConsumer(rid).consume(response))

    tool_completes = [e for e in events if isinstance(e, LLMToolCallMessage)]
    assert len(tool_completes) == 1
    call = tool_completes[0].call
    assert call.name == "search"
    assert call.args == {"q": "x"}

    result = events[-1]
    assert isinstance(result, LLMTotalMessage)
    assert result.finish_reason is FinishReason.TOOL_CALLS


def test_non_stream_reasoning_from_model_extra() -> None:
    rid = new_request_id()
    response = _completion(
        {"role": "assistant", "content": "answer", "reasoning_content": "because"},
    )

    events = list(ChatCompletionConsumer(rid).consume(response))

    thinking = [e for e in events if isinstance(e, LLMThinkingMessage)]
    assert len(thinking) == 1
    assert thinking[0].content == "because"


# --- stream --------------------------------------------------------------- #


def test_stream_emits_deltas_and_snapshots() -> None:
    rid = new_request_id()
    ctx = LLMContext(request=LLMRequest(request_id=rid, model="test-model"))
    chunks = [
        _chunk({"role": "assistant", "content": "he"}),
        _chunk({"content": "llo"}),
        _chunk({}, finish_reason="stop"),
    ]

    events = list(ChatCompletionChunkConsumer(rid).stream(ctx, chunks))

    deltas = [e for e in events if isinstance(e, LLMAnswerDelta)]
    assert [d.token for d in deltas] == ["he", "llo"]

    completes = [e for e in events if isinstance(e, LLMAnswerMessage)]
    assert len(completes) == 1
    assert completes[0].content == "hello"

    result = events[-1]
    assert isinstance(result, LLMTotalMessage)
    assert result.message.content == "hello"
    assert result.finish_reason is FinishReason.STOP


def test_stream_flushes_thinking_snapshot_before_answer_deltas() -> None:
    """thinking-слот закрывается инлайн — по переходу к answer, до его дельт."""
    rid = new_request_id()
    ctx = LLMContext(request=LLMRequest(request_id=rid, model="test-model"))
    chunks = [
        _chunk({"role": "assistant", "reasoning_content": "be"}),
        _chunk({"reasoning_content": "cause"}),
        _chunk({"content": "ans"}),
        _chunk({}, finish_reason="stop"),
    ]

    events = list(ChatCompletionChunkConsumer(rid).stream(ctx, chunks))
    types = [type(e) for e in events]

    # ThinkingMessage эмитится после thinking-дельт, но ДО первой answer-дельты.
    thinking_snapshot = types.index(LLMThinkingMessage)
    first_answer_delta = types.index(LLMAnswerDelta)
    assert thinking_snapshot < first_answer_delta

    thinking = next(e for e in events if isinstance(e, LLMThinkingMessage))
    assert thinking.content == "because"
    # порядок: thinking-дельты, ThinkingMessage, answer-дельта, AnswerMessage, result
    assert types == [
        LLMThinkingDelta,
        LLMThinkingDelta,
        LLMThinkingMessage,
        LLMAnswerDelta,
        LLMAnswerMessage,
        LLMTotalMessage,
    ]


def test_stream_flushes_answer_snapshot_before_tool_call_deltas() -> None:
    """answer-слот закрывается по переходу к tool_calls, не дожидаясь args."""
    rid = new_request_id()
    ctx = LLMContext(request=LLMRequest(request_id=rid, model="test-model"))
    chunks = [
        _chunk({"role": "assistant", "content": "hi"}),
        _chunk(
            {
                "tool_calls": [
                    {
                        "index": 0,
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "search", "arguments": '{"q'},
                    },
                ],
            },
        ),
        _chunk(
            {
                "tool_calls": [
                    {"index": 0, "function": {"arguments": '": "x"}'}},
                ],
            },
        ),
        _chunk({}, finish_reason="tool_calls"),
    ]

    events = list(ChatCompletionChunkConsumer(rid).stream(ctx, chunks))
    types = [type(e) for e in events]

    # AnswerMessage закрылся до того, как args тула достримились до конца.
    answer_snapshot = types.index(LLMAnswerMessage)
    last_tool_delta = len(types) - 1 - types[::-1].index(LLMToolCallDelta)
    assert answer_snapshot < last_tool_delta

    # Тул-снапшот — на finish, после всех tool-дельт; result последним.
    assert isinstance(events[-1], LLMTotalMessage)
    assert types[-2] == LLMToolCallMessage
    tool_msg = events[-2]
    assert isinstance(tool_msg, LLMToolCallMessage)
    assert tool_msg.call.args == {"q": "x"}


# --- fallback: tool-call в content --------------------------------------- #


def test_non_stream_fallback_remaps_tool_call_from_content() -> None:
    """content с JSON-вызовом -> tool_calls в итоге; текстового answer нет."""
    rid = new_request_id()
    response = _completion(
        {
            "role": "assistant",
            "content": '{"name": "search", "arguments": {"q": "x"}}',
        },
    )

    events = list(
        ChatCompletionConsumer(rid, ToolCallFromContentFallback()).consume(response)
    )

    assert not any(isinstance(e, LLMAnswerMessage) for e in events)
    tool_msgs = [e for e in events if isinstance(e, LLMToolCallMessage)]
    assert len(tool_msgs) == 1
    assert tool_msgs[0].call.name == "search"
    assert tool_msgs[0].call.args == {"q": "x"}

    result = events[-1]
    assert isinstance(result, LLMTotalMessage)
    assert result.message.content == ""
    assert [c.name for c in result.message.tool_calls] == ["search"]


def test_non_stream_without_fallback_keeps_content_as_answer() -> None:
    """Без декодера тот же content остаётся обычным текстовым ответом."""
    rid = new_request_id()
    response = _completion(
        {
            "role": "assistant",
            "content": '{"name": "search", "arguments": {"q": "x"}}',
        },
    )

    events = list(ChatCompletionConsumer(rid).consume(response))

    assert any(isinstance(e, LLMAnswerMessage) for e in events)
    assert not any(isinstance(e, LLMToolCallMessage) for e in events)


def test_stream_fallback_remaps_tool_call_from_content() -> None:
    """В стриме content собирается дельтами, затем перемапится в tool_calls."""
    rid = new_request_id()
    ctx = LLMContext(request=LLMRequest(request_id=rid, model="test-model"))
    chunks = [
        _chunk({"role": "assistant", "content": '{"name": "search", '}),
        _chunk({"content": '"arguments": {"q": "x"}}'}),
        _chunk({}, finish_reason="stop"),
    ]

    events = list(
        ChatCompletionChunkConsumer(rid, ToolCallFromContentFallback()).stream(
            ctx, chunks
        )
    )

    # текстовый answer-снапшот подавлен, вместо него tool-call
    assert not any(isinstance(e, LLMAnswerMessage) for e in events)
    tool_msgs = [e for e in events if isinstance(e, LLMToolCallMessage)]
    assert len(tool_msgs) == 1
    assert tool_msgs[0].call.name == "search"
    assert tool_msgs[0].call.args == {"q": "x"}

    result = events[-1]
    assert isinstance(result, LLMTotalMessage)
    assert result.message.content == ""
    assert [c.name for c in result.message.tool_calls] == ["search"]


def test_stream_fallback_skips_when_native_tool_calls_present() -> None:
    """Если провайдер прислал нативный tool_calls — content не трогаем."""
    rid = new_request_id()
    ctx = LLMContext(request=LLMRequest(request_id=rid, model="test-model"))
    chunks = [
        _chunk({"role": "assistant", "content": "hi"}),
        _chunk(
            {
                "tool_calls": [
                    {
                        "index": 0,
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "native", "arguments": "{}"},
                    },
                ],
            },
        ),
        _chunk({}, finish_reason="tool_calls"),
    ]

    events = list(
        ChatCompletionChunkConsumer(rid, ToolCallFromContentFallback()).stream(
            ctx, chunks
        )
    )

    result = events[-1]
    assert isinstance(result, LLMTotalMessage)
    assert [c.name for c in result.message.tool_calls] == ["native"]
    assert result.message.content == "hi"
