"""Декодер потока OpenAI chunks в поток LLMEvent."""

from __future__ import annotations

from collections.abc import Iterable

from boba.adapter.openai.observer import MultiKeyReasoningExtractor
from boba.domain.core.patterns import (
    Converter,
    StreamTransformer,
    StreamTransformerPipeline,
)
from boba.domain.llm.errors import LLMProtocolError
from boba.domain.llm.events import (
    FinishReason,
    LLMAnswerStarted,
    LLMAnswerToken,
    LLMEvent,
    LLMGenerationDone,
    LLMGenerationStarted,
    LLMRefusalToken,
    LLMThinkingStarted,
    LLMThinkingToken,
    LLMToolCallArgumentDelta,
    LLMToolCallBegin,
)
from boba.domain.llm.models import LLMContext, RequestId
from openai.types.chat.chat_completion_chunk import (
    ChatCompletionChunk,
    Choice,
    ChoiceDelta,
)


class ThinkingSource(StreamTransformer[LLMContext, Choice, LLMEvent]):
    """Эмитит LLMThinkingStarted / LLMThinkingToken."""

    def __init__(
        self,
        request_id: RequestId,
        extractor: Converter[ChoiceDelta, str | None],
    ) -> None:
        self._request_id = request_id
        self._extractor = extractor
        self._started = False

    def name(self) -> str:
        return "Thinking"

    def reset(self) -> None:
        self._started = False

    def stream(self, ctx: LLMContext, stream: Iterable[Choice]) -> Iterable[LLMEvent]:
        for choice in stream:
            thinking = self._extractor.convert(choice.delta)
            if thinking:
                if not self._started:
                    self._started = True
                    yield LLMThinkingStarted(request_id=self._request_id)
                yield LLMThinkingToken(request_id=self._request_id, token=thinking)


class RoleSource(StreamTransformer[LLMContext, Choice, LLMEvent]):
    """Эмитит LLMGenerationStarted при первом появлении роли."""

    def __init__(self, request_id: RequestId) -> None:
        self._request_id = request_id
        self._started = False

    def name(self) -> str:
        return "Role"

    def reset(self) -> None:
        self._started = False

    def stream(self, ctx: LLMContext, stream: Iterable[Choice]) -> Iterable[LLMEvent]:
        # Роль проверяем только при первом проходе.
        if not self._started:
            # TODO: несколько choice'ов сейчас обрабатываются последовательно.
            for choice in stream:
                if choice.delta.role and not self._started:
                    self._started = True
                    yield LLMGenerationStarted(request_id=self._request_id)


class AnswerSource(StreamTransformer[LLMContext, Choice, LLMEvent]):
    """Эмитит LLMAnswerStarted / LLMAnswerToken из content."""

    def __init__(self, request_id: RequestId) -> None:
        self._request_id = request_id
        self._started = False

    def name(self) -> str:
        return "Answer"

    def reset(self) -> None:
        self._started = False

    def stream(self, ctx: LLMContext, stream: Iterable[Choice]) -> Iterable[LLMEvent]:
        for choice in stream:
            if choice.delta.content:
                if not self._started:
                    self._started = True
                    yield LLMAnswerStarted(request_id=self._request_id)
                yield LLMAnswerToken(
                    request_id=self._request_id, token=choice.delta.content
                )


class RefusalSource(StreamTransformer[LLMContext, Choice, LLMEvent]):
    """Эмитит LLMRefusalToken из refusal."""

    def __init__(self, request_id: RequestId) -> None:
        self._request_id = request_id

    def name(self) -> str:
        return "Refusal"

    def stream(self, ctx: LLMContext, stream: Iterable[Choice]) -> Iterable[LLMEvent]:
        for choice in stream:
            if choice.delta.refusal:
                yield LLMRefusalToken(
                    request_id=self._request_id, token=choice.delta.refusal
                )


class ToolCallSource(StreamTransformer[LLMContext, Choice, LLMEvent]):
    """Эмитит LLMToolCallBegin / LLMToolCallArgumentDelta."""

    def __init__(self, request_id: RequestId) -> None:
        self._request_id = request_id
        self._seen: set[int] = set()

    def name(self) -> str:
        return "ToolCall"

    def reset(self) -> None:
        self._seen.clear()

    def stream(self, ctx: LLMContext, stream: Iterable[Choice]) -> Iterable[LLMEvent]:
        for choice in stream:
            if not choice.delta.tool_calls:
                continue
            for tc in choice.delta.tool_calls:
                if (
                    tc.index not in self._seen
                    and tc.id
                    and tc.function
                    and tc.function.name
                ):
                    self._seen.add(tc.index)
                    yield LLMToolCallBegin(
                        request_id=self._request_id,
                        index=tc.index,
                        tool_call_id=tc.id,
                        tool_name=tc.function.name,
                    )
                if tc.function and tc.function.arguments:
                    yield LLMToolCallArgumentDelta(
                        request_id=self._request_id,
                        index=tc.index,
                        arguments=tc.function.arguments,
                    )


class FinishSource(StreamTransformer[LLMContext, Choice, LLMEvent]):
    """Эмитит LLMGenerationDone при появлении finish_reason."""

    def __init__(self, request_id: RequestId) -> None:
        self._request_id = request_id
        self._saw_tool_call = False

    def name(self) -> str:
        return "Finish"

    def reset(self) -> None:
        self._saw_tool_call = False

    def stream(self, ctx: LLMContext, stream: Iterable[Choice]) -> Iterable[LLMEvent]:
        """Подменяет finish_reason=stop на tool_calls если были tool_calls."""
        for choice in stream:
            if choice.delta.tool_calls:
                self._saw_tool_call = True

            if choice.finish_reason:
                try:
                    reason = FinishReason(choice.finish_reason)
                except ValueError as e:
                    raise LLMProtocolError(
                        f"unknown finish_reason from provider: {choice.finish_reason!r}"
                    ) from e

                # Lifehack: если были tool_calls, принудительно ставим TOOL_CALLS.
                if self._saw_tool_call and reason is not FinishReason.TOOL_CALLS:
                    reason = FinishReason.TOOL_CALLS

                yield LLMGenerationDone(
                    request_id=self._request_id,
                    finish_reason=reason,
                )


class FromOpenAIChunkConverter(
    StreamTransformer[LLMContext, ChatCompletionChunk, LLMEvent]
):
    """Поток OpenAI chunks → поток LLMEvent."""

    def __init__(
        self,
        request_id: RequestId,
    ) -> None:
        # Строгая последовательность вызова.
        self._pipeline = StreamTransformerPipeline[LLMContext, Choice, LLMEvent](
            [
                RoleSource(request_id),
                ThinkingSource(request_id, MultiKeyReasoningExtractor()),
                AnswerSource(request_id),
                RefusalSource(request_id),
                ToolCallSource(request_id),
                FinishSource(request_id),
            ]
        )

    def name(self) -> str:
        return f"FromOpenAIChunkConverter({self._pipeline.name()})"

    def reset(self) -> None:
        self._pipeline.reset()

    def stream(
        self, ctx: LLMContext, stream: Iterable[ChatCompletionChunk]
    ) -> Iterable[LLMEvent]:
        for chunk in stream:
            yield from self._pipeline.stream(ctx, chunk.choices)
