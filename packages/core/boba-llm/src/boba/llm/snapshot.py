"""Разворачивание финального AssistantMessage в поток итоговых LLM-событий."""

from __future__ import annotations

from collections.abc import Iterator

from boba.llm.events import (
    FinishReason,
    LLMAnswerComplete,
    LLMEvent,
    LLMGenerationResult,
    LLMInvalidToolCallReceived,
    LLMRefusalComplete,
    LLMThinkingComplete,
    LLMToolCallComplete,
)
from boba.llm.models import AssistantMessage, RequestId

__all__ = ["SnapshotEmitter"]


class SnapshotEmitter:
    """
    Финальное сообщение сформированное llm - LLMGenerationResult

    Единая точка формирующая законченные события,
    как для stream, так и для non-stream-консьюмеров
    """

    @staticmethod
    def emit(
        request_id: RequestId,
        message: AssistantMessage,
        finish_reason: FinishReason,
    ) -> Iterator[LLMEvent]:
        if message.thinking:
            yield LLMThinkingComplete(request_id=request_id, content=message.thinking)

        if message.content:
            yield LLMAnswerComplete(request_id=request_id, content=message.content)

        if message.refusal:
            yield LLMRefusalComplete(request_id=request_id, content=message.refusal)

        for call in message.tool_calls:
            yield LLMToolCallComplete(request_id=request_id, call=call)

        for invalid in message.invalid_tool_calls:
            yield LLMInvalidToolCallReceived(request_id=request_id, invalid=invalid)

        yield LLMGenerationResult(
            request_id=request_id,
            message=message,
            finish_reason=finish_reason,
        )
