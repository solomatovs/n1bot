"""Тесты консьюмеров ответа OpenAI: stream (chunks) и non-stream (ChatCompletion)."""

from __future__ import annotations

from typing import Any

from openai.types.chat import ChatCompletion
from openai.types.chat.chat_completion_chunk import ChatCompletionChunk

from boba.llm.events import (
    FinishReason,
    LLMAnswerDelta,
    LLMAnswerMessage,
    LLMGenerationResult,
    LLMThinkingMessage,
    LLMToolCallMessage,
)
from boba.llm.models import LLMContext, LLMRequest, new_request_id
from boba.provider.openai.response import (
    ChatCompletionChunkConsumer,
    ChatCompletionConsumer,
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
    assert [type(e) for e in events] == [LLMAnswerMessage, LLMGenerationResult]

    answer, result = events
    assert isinstance(answer, LLMAnswerMessage)
    assert answer.content == "hello"
    assert isinstance(result, LLMGenerationResult)
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
    assert isinstance(result, LLMGenerationResult)
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
    assert isinstance(result, LLMGenerationResult)
    assert result.message.content == "hello"
    assert result.finish_reason is FinishReason.STOP
