"""Декодер потока OpenAI chunks в поток :class:`LLMEvent`.

Стадии-декодеры независимы (fan-out) и собраны через
:class:`~boba.domain.core.patterns.StreamTransformerPipeline`. Каждая
смотрит на «свою» часть ``Choice.delta`` и эмитит соответствующие
LLM-события.

``MultiKeyReasoningExtractor`` — локальная копия старого экстрактора
из :mod:`boba.adapters.raw_llm_observer`. Копируем (а не импортируем)
по политике изоляции: слой ``boba_2`` не зависит от адаптеров
``boba.adapters.*``.
"""

from __future__ import annotations

from collections.abc import Iterable

from openai.types.chat.chat_completion_chunk import (
    ChatCompletionChunk,
    Choice,
    ChoiceDelta,
)

from boba.domain.core.patterns import (
    Converter,
    StreamTransformer,
    StreamTransformerPipeline,
)
from boba_2.domain.llm.errors import LLMProtocolError
from boba_2.domain.llm.events import (
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
from boba_2.domain.llm.models import LLMContext, RequestId


class MultiKeyReasoningExtractor(Converter[ChoiceDelta, str | None]):
    """Извлекает reasoning-токен из ``delta.model_extra``, перебирая
    известные ключи по порядку.

    Разные провайдеры кладут «рассуждения» модели в разные поля:

    - ``reasoning_content`` — DeepSeek, xAI Grok, часть OpenAI-compat прокси;
    - ``thinking`` — Anthropic через openai-compat, некоторые LiteLLM-маршруты;
    - ``reasoning`` — Ollama native, Groq.
    """

    DEFAULT_KEYS: tuple[str, ...] = (
        "reasoning_content",
        "thinking",
        "reasoning",
    )

    def __init__(self, keys: tuple[str, ...] | None = None) -> None:
        self._keys = keys if keys is not None else self.DEFAULT_KEYS

    def convert(self, value: ChoiceDelta) -> str | None:
        extra = value.model_extra or {}
        for k in self._keys:
            v = extra.get(k)
            if v:
                return str(v)
        return None


class RoleSource(StreamTransformer[LLMContext, Choice, LLMEvent]):
    """Эмитит :class:`LLMGenerationStarted` при первом появлении роли."""

    def __init__(self, request_id: RequestId) -> None:
        self._request_id = request_id
        self._started = False

    def name(self) -> str:
        return "Role"

    def reset(self) -> None:
        self._started = False

    def stream(
        self, ctx: LLMContext, stream: Iterable[Choice]
    ) -> Iterable[LLMEvent]:
        if not self._started:
            for choice in stream:
                if choice.delta.role and not self._started:
                    self._started = True
                    yield LLMGenerationStarted(request_id=self._request_id)


class ThinkingSource(StreamTransformer[LLMContext, Choice, LLMEvent]):
    """Эмитит :class:`LLMThinkingStarted` / :class:`LLMThinkingToken`.

    Извлечение reasoning-поля делегируется ``Converter[ChoiceDelta,
    str | None]`` — провайдер-специфичный вариант подключается через DI.
    """

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

    def stream(
        self, ctx: LLMContext, stream: Iterable[Choice]
    ) -> Iterable[LLMEvent]:
        for choice in stream:
            thinking = self._extractor.convert(choice.delta)
            if thinking:
                if not self._started:
                    self._started = True
                    yield LLMThinkingStarted(request_id=self._request_id)
                yield LLMThinkingToken(
                    request_id=self._request_id, token=thinking
                )


class AnswerSource(StreamTransformer[LLMContext, Choice, LLMEvent]):
    """Эмитит :class:`LLMAnswerStarted` / :class:`LLMAnswerToken` из content."""

    def __init__(self, request_id: RequestId) -> None:
        self._request_id = request_id
        self._started = False

    def name(self) -> str:
        return "Answer"

    def reset(self) -> None:
        self._started = False

    def stream(
        self, ctx: LLMContext, stream: Iterable[Choice]
    ) -> Iterable[LLMEvent]:
        for choice in stream:
            if choice.delta.content:
                if not self._started:
                    self._started = True
                    yield LLMAnswerStarted(request_id=self._request_id)
                yield LLMAnswerToken(
                    request_id=self._request_id, token=choice.delta.content
                )


class RefusalSource(StreamTransformer[LLMContext, Choice, LLMEvent]):
    """Эмитит :class:`LLMRefusalToken` из refusal."""

    def __init__(self, request_id: RequestId) -> None:
        self._request_id = request_id

    def name(self) -> str:
        return "Refusal"

    def stream(
        self, ctx: LLMContext, stream: Iterable[Choice]
    ) -> Iterable[LLMEvent]:
        for choice in stream:
            if choice.delta.refusal:
                yield LLMRefusalToken(
                    request_id=self._request_id, token=choice.delta.refusal
                )


class ToolCallSource(StreamTransformer[LLMContext, Choice, LLMEvent]):
    """Эмитит :class:`LLMToolCallBegin` / :class:`LLMToolCallArgumentDelta`."""

    def __init__(self, request_id: RequestId) -> None:
        self._request_id = request_id
        self._seen: set[int] = set()

    def name(self) -> str:
        return "ToolCall"

    def reset(self) -> None:
        self._seen.clear()

    def stream(
        self, ctx: LLMContext, stream: Iterable[Choice]
    ) -> Iterable[LLMEvent]:
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
    """Эмитит :class:`LLMGenerationDone` при появлении finish_reason.

    Учитывает baghack Ollama: ``finish_reason=stop`` после native
    tool_calls подменяется на ``tool_calls`` — иначе агент не поймёт,
    что нужно выполнить tool и продолжить цикл.
    """

    def __init__(self, request_id: RequestId) -> None:
        self._request_id = request_id
        self._saw_tool_call = False

    def name(self) -> str:
        return "Finish"

    def reset(self) -> None:
        self._saw_tool_call = False

    def stream(
        self, ctx: LLMContext, stream: Iterable[Choice]
    ) -> Iterable[LLMEvent]:
        for choice in stream:
            if choice.delta.tool_calls:
                self._saw_tool_call = True
            if choice.finish_reason:
                try:
                    reason = FinishReason(choice.finish_reason)
                except ValueError as e:
                    raise LLMProtocolError(
                        f"unknown finish_reason from provider: "
                        f"{choice.finish_reason!r}"
                    ) from e
                if self._saw_tool_call and reason is not FinishReason.TOOL_CALLS:
                    reason = FinishReason.TOOL_CALLS
                yield LLMGenerationDone(
                    request_id=self._request_id,
                    finish_reason=reason,
                )


class FromOpenAIChunkConverter(
    StreamTransformer[LLMContext, ChatCompletionChunk, LLMEvent]
):
    """Поток OpenAI chunks → поток :class:`LLMEvent`.

    Внутри — fan-out pipeline независимых декодеров
    (role/thinking/answer/refusal/tool_call/finish). Каждый смотрит на
    «свою» часть ``Choice.delta`` и эмитит соответствующие события.
    """

    def __init__(
        self,
        request_id: RequestId,
        reasoning_extractor: Converter[ChoiceDelta, str | None] | None = None,
    ) -> None:
        extractor = reasoning_extractor or MultiKeyReasoningExtractor()
        self._pipeline = StreamTransformerPipeline[LLMContext, Choice, LLMEvent](
            [
                RoleSource(request_id),
                ThinkingSource(request_id, extractor),
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
            yield from self._pipeline.stream(ctx, list(chunk.choices))
