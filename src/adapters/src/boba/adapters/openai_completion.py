"""OpenAI-совместимая реализация LLM — terminal и middleware."""

from __future__ import annotations

import logging
import time
from collections.abc import Iterable
from typing import Any

from openai import OpenAI
from openai.types.chat import (
    ChatCompletionAssistantMessageParam,
    ChatCompletionMessageParam,
    ChatCompletionSystemMessageParam,
    ChatCompletionToolMessageParam,
    ChatCompletionToolParam,
    ChatCompletionUserMessageParam,
)
from openai.types.chat.chat_completion_chunk import (
    ChatCompletionChunk,
    Choice,
)

from boba.domain.agent.events import (
    AgentEvent,
    AnswerStarted,
    AnswerToken,
    GenerationDone,
    GenerationStarted,
    RefusalToken,
    ThinkingStarted,
    ThinkingToken,
    ToolCallArgumentDelta,
    ToolCallBegin,
)
from boba.domain.agent.llm_request_factory import LLMRequestFactory
from boba.domain.agent.models import (
    AgentContext,
    LLMMessage,
    LLMRequest,
    RequestId,
)
from boba.domain.config import LLMConfig
from boba.domain.core.patterns import (
    Converter,
    StreamConverter,
    StreamSource,
    StreamTransformer,
    StreamTransformerPipeline,
)
from boba.domain.core.tools import Tool

logger = logging.getLogger(__name__)


class LoggingLLMMiddleware(StreamSource[AgentContext, AgentEvent]):
    """Логирует запрос, количество событий и время генерации."""

    def __init__(self, inner: StreamSource[AgentContext, AgentEvent]) -> None:
        self._inner = inner

    def name(self) -> str:
        return "LoggingLLM"

    def stream(self, ctx: AgentContext) -> Iterable[AgentEvent]:
        logger.info("LLM request: model=%s", ctx.request.model)
        start = time.monotonic()
        count = 0

        for event in self._inner.stream(ctx):
            count += 1
            yield event

        elapsed = time.monotonic() - start
        logger.info("LLM done: %d events in %.2fs", count, elapsed)


class StupidRetryLLMMiddleware(StreamSource[AgentContext, AgentEvent]):
    """Повторяет запрос при ошибке до max_retries раз."""

    def __init__(
        self, inner: StreamSource[AgentContext, AgentEvent], max_retries: int = 3
    ) -> None:
        self._inner = inner
        self._max_retries = max_retries

    def name(self) -> str:
        return "RetryLLM"

    def stream(self, ctx: AgentContext) -> Iterable[AgentEvent]:
        for attempt in range(self._max_retries):
            try:
                yield from self._inner.stream(ctx)
                return
            except Exception as e:
                if attempt == self._max_retries - 1:
                    raise
                logger.warning(
                    "LLM attempt %d/%d failed, retrying: %s",
                    attempt + 1,
                    self._max_retries,
                    e,
                )


class FromOpenAIChunkConverter(
    StreamTransformer[AgentContext, ChatCompletionChunk, AgentEvent]
):
    """
    Конвертирует поток OpenAI chunks в поток AgentEvent.
    Делегирует обработку подключаемым StreamTransformer-ам через pipeline.
    """

    def __init__(self, request_id: RequestId) -> None:
        self._pipeline = StreamTransformerPipeline[AgentContext, Choice, AgentEvent](
            [
                RoleSource(request_id),
                ThinkingSource(request_id),
                AnswerSource(request_id),
                RefusalSource(request_id),
                ToolCallSource(request_id),
                FinishSource(request_id),
            ]
        )

    def name(self) -> str:
        return f"FromOpenAIChunkConverter({self._pipeline.name()})"

    def stream(
        self, ctx: AgentContext, stream: Iterable[ChatCompletionChunk]
    ) -> Iterable[AgentEvent]:
        for chunk in stream:
            yield from self._pipeline.stream(ctx, chunk.choices)


class OpenAIMiddleware(StreamSource[AgentContext, AgentEvent]):
    """
    Terminal — вызывает OpenAI-совместимый API.
    Получает готовый :class:`LLMRequest` от :class:`LLMRequestFactory`,
    мапит его в kwargs провайдера через :class:`ToOpenAIRequestConverter`
    и стримит :class:`AgentEvent`.
    """

    def __init__(
        self,
        config: LLMConfig,
        llm_request_factory: LLMRequestFactory,
    ) -> None:
        self._client = OpenAI(base_url=config.base_url, api_key=config.api_key)
        self._llm_request_factory = llm_request_factory
        self._to_request_converter = ToOpenAIRequestConverter()

    def name(self) -> str:
        return "OpenAICompletion"

    def stream(self, ctx: AgentContext) -> Iterable[AgentEvent]:
        llm_request = self._llm_request_factory.build(ctx)
        kwargs = self._to_request_converter.convert(llm_request)

        response = self._client.chat.completions.create(**kwargs)

        yield from FromOpenAIChunkConverter(ctx.request.request_id).stream(
            ctx, response
        )


class ToOpenAIRequestConverter(Converter[LLMRequest, dict[str, Any]]):
    """Мапит :class:`LLMRequest` в kwargs для
    ``client.chat.completions.create``.
    """

    def __init__(self) -> None:
        self._to_message_converter = ToOpenAIMessageConverter()
        self._to_tool_converter = ToOpenAIToolConverter()

    def convert(self, value: LLMRequest) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "model": value.model,
            "messages": list(self._to_message_converter.convert(value.messages)),
            "stream": True,
        }

        if value.tools:
            kwargs["tools"] = [self._to_tool_converter.convert(t) for t in value.tools]
        if value.tool_choice is not None:
            kwargs["tool_choice"] = value.tool_choice
        if value.response_format is not None:
            kwargs["response_format"] = value.response_format

        s = value.sampling
        if s.temperature is not None:
            kwargs["temperature"] = s.temperature
        if s.top_p is not None:
            kwargs["top_p"] = s.top_p
        if s.max_tokens is not None:
            kwargs["max_tokens"] = s.max_tokens
        if s.seed is not None:
            kwargs["seed"] = s.seed
        if s.stop is not None:
            kwargs["stop"] = s.stop

        return kwargs


class ToOpenAIToolConverter(Converter[Tool[Any], ChatCompletionToolParam]):
    """Конвертирует Tool в формат OpenAI tools API."""

    def convert(self, value: Tool[Any]) -> ChatCompletionToolParam:
        definition = value.definition()

        properties: dict[str, dict[str, str]] = {}
        required: list[str] = []

        for p in definition.input_schema.params:
            properties[p.name] = {
                "type": p.type.value,
                "description": p.description,
            }
            if p.required:
                required.append(p.name)

        return {
            "type": "function",
            "function": {
                "name": value.tool_id().to_wire(),
                "description": definition.description,
                "parameters": {
                    "type": "object",
                    "properties": properties,
                    "required": required,
                },
            },
        }


class ToOpenAIOneMessageConverter(Converter[LLMMessage, ChatCompletionMessageParam]):
    """Конвертирует LLMMessage в формат OpenAI API."""

    def convert(self, value: LLMMessage) -> ChatCompletionMessageParam:
        match value.role:
            case "system":
                return ChatCompletionSystemMessageParam(
                    role="system",
                    content=value.content,
                )
            case "user":
                return ChatCompletionUserMessageParam(
                    role="user",
                    content=value.content,
                )
            case "assistant":
                param = ChatCompletionAssistantMessageParam(
                    role="assistant",
                    content=value.content,
                )
                if value.tool_calls:
                    param["tool_calls"] = [
                        {
                            "id": tc.id,
                            "type": "function",
                            "function": {"name": tc.name, "arguments": tc.arguments},
                        }
                        for tc in value.tool_calls
                    ]
                return param
            case "tool":
                return ChatCompletionToolMessageParam(
                    role="tool",
                    content=value.content,
                    tool_call_id=value.tool_call_id or "",
                )
            case _:
                raise ValueError(f"Unknown message role: {value.role}")


class ToOpenAIMessageConverter(StreamConverter[LLMMessage, ChatCompletionMessageParam]):
    """Конвертирует LLMMessage в формат OpenAI API."""

    def __init__(self) -> None:
        self._converter = ToOpenAIOneMessageConverter()

    def set_request_id(self, request_id: RequestId) -> None:
        self._request_id = request_id

    def convert(
        self, stream: Iterable[LLMMessage]
    ) -> Iterable[ChatCompletionMessageParam]:
        for item in stream:
            yield self._converter.convert(item)


class RoleSource(StreamTransformer[AgentContext, Choice, AgentEvent]):
    """Порождает GenerationStarted при первом появлении роли."""

    def __init__(self, request_id: RequestId) -> None:
        self._request_id = request_id
        self._started = False

    def name(self) -> str:
        return "Role"

    def reset(self) -> None:
        self._started = False

    def stream(
        self, ctx: AgentContext, stream: Iterable[Choice]
    ) -> Iterable[AgentEvent]:
        if not self._started:
            for choice in stream:
                if choice.delta.role and not self._started:
                    self._started = True
                    yield GenerationStarted(request_id=self._request_id)


class ThinkingSource(StreamTransformer[AgentContext, Choice, AgentEvent]):
    """Порождает ThinkingStarted/ThinkingToken из reasoning_content."""

    def __init__(self, request_id: RequestId) -> None:
        self._request_id = request_id
        self._started = False

    def name(self) -> str:
        return "Thinking"

    def reset(self) -> None:
        self._started = False

    def stream(
        self, ctx: AgentContext, stream: Iterable[Choice]
    ) -> Iterable[AgentEvent]:
        for choice in stream:
            extra = choice.delta.model_extra or {}
            thinking = extra.get("reasoning_content") or extra.get("thinking")

            if thinking:
                if not self._started:
                    self._started = True
                    yield ThinkingStarted(request_id=self._request_id)

                yield ThinkingToken(request_id=self._request_id, token=thinking)


class AnswerSource(StreamTransformer[AgentContext, Choice, AgentEvent]):
    """Порождает AnswerStarted/AnswerToken из content."""

    def __init__(self, request_id: RequestId) -> None:
        self._request_id = request_id
        self._started = False

    def name(self) -> str:
        return "Answer"

    def reset(self) -> None:
        self._started = False

    def stream(
        self, ctx: AgentContext, stream: Iterable[Choice]
    ) -> Iterable[AgentEvent]:
        for choice in stream:
            if choice.delta.content:
                if not self._started:
                    self._started = True
                    yield AnswerStarted(request_id=self._request_id)
                yield AnswerToken(
                    request_id=self._request_id, token=choice.delta.content
                )


class RefusalSource(StreamTransformer[AgentContext, Choice, AgentEvent]):
    """Порождает RefusalToken из refusal."""

    def __init__(self, request_id: RequestId) -> None:
        self._request_id = request_id

    def name(self) -> str:
        return "Refusal"

    def stream(
        self, ctx: AgentContext, stream: Iterable[Choice]
    ) -> Iterable[AgentEvent]:
        for choice in stream:
            if choice.delta.refusal:
                yield RefusalToken(
                    request_id=self._request_id, token=choice.delta.refusal
                )


class ToolCallSource(StreamTransformer[AgentContext, Choice, AgentEvent]):
    """Порождает ToolCallBegin/ToolCallArgumentDelta из tool_calls."""

    def __init__(self, request_id: RequestId) -> None:
        self._request_id = request_id
        self._seen: set[int] = set()

    def name(self) -> str:
        return "ToolCall"

    def reset(self) -> None:
        self._seen.clear()

    def stream(
        self, ctx: AgentContext, stream: Iterable[Choice]
    ) -> Iterable[AgentEvent]:
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
                    yield ToolCallBegin(
                        request_id=self._request_id,
                        index=tc.index,
                        tool_call_id=tc.id,
                        tool_name=tc.function.name,
                    )
                if tc.function and tc.function.arguments:
                    yield ToolCallArgumentDelta(
                        request_id=self._request_id,
                        index=tc.index,
                        arguments=tc.function.arguments,
                    )


class FinishSource(StreamTransformer[AgentContext, Choice, AgentEvent]):
    """Порождает GenerationDone при finish_reason."""

    def __init__(self, request_id: RequestId) -> None:
        self._request_id = request_id

    def name(self) -> str:
        return "Finish"

    def stream(
        self, ctx: AgentContext, stream: Iterable[Choice]
    ) -> Iterable[AgentEvent]:
        for choice in stream:
            if choice.finish_reason:
                yield GenerationDone(
                    request_id=self._request_id,
                    finish_reason=choice.finish_reason,
                )
