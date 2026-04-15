"""OpenAI-совместимая реализация LLM — terminal и middleware."""

from __future__ import annotations

import logging
import time
from typing import Iterator

from openai import OpenAI
from openai.types.chat import (
    ChatCompletionAssistantMessageParam,
    ChatCompletionMessageParam,
    ChatCompletionSystemMessageParam,
    ChatCompletionToolMessageParam,
    ChatCompletionUserMessageParam,
)

from boba.domain.config import LLMConfig
from boba.domain.agent.llm import LLMMiddleware
from boba.domain.agent.models import AgentContext, LLMMessage, RequestId
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
from boba.domain.core.messages import MessageService
from boba.domain.core.patterns import Converter, StreamConverter

from openai.types.chat.chat_completion_chunk import ChatCompletionChunk

logger = logging.getLogger(__name__)


class LoggingLLMMiddleware(LLMMiddleware):
    """Логирует запрос, количество событий и время генерации."""

    def __init__(self, next: LLMMiddleware) -> None:
        self._next = next

    def name(self) -> str:
        return "LoggingLLM"

    def produce(self, ctx: AgentContext) -> Iterator[AgentEvent]:
        logger.info("LLM request: model=%s", ctx.request.model)
        start = time.monotonic()
        count = 0

        for event in self._next.produce(ctx):
            count += 1
            yield event

        elapsed = time.monotonic() - start
        logger.info("LLM done: %d events in %.2fs", count, elapsed)


class StupidRetryLLMMiddleware(LLMMiddleware):
    """Повторяет запрос при ошибке до max_retries раз."""

    def __init__(self, next: LLMMiddleware, max_retries: int = 3) -> None:
        self._next = next
        self._max_retries = max_retries

    def name(self) -> str:
        return "RetryLLM"

    def produce(self, ctx: AgentContext) -> Iterator[AgentEvent]:
        for attempt in range(self._max_retries):
            try:
                yield from self._next.produce(ctx)
                return
            except Exception:
                if attempt == self._max_retries - 1:
                    raise
                logger.warning(
                    "LLM attempt %d/%d failed, retrying",
                    attempt + 1,
                    self._max_retries,
                )


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
        self, stream: Iterator[LLMMessage]
    ) -> Iterator[ChatCompletionMessageParam]:
        for item in stream:
            yield self._converter.convert(item)


class FromOpenAIChunkConverter(StreamConverter[ChatCompletionChunk, AgentEvent]):
    """
    Конвертирует поток OpenAI chunks в поток AgentEvent.
    С состоянием: отслеживает started и seen tool call indices.
    Вызвать reset() перед повторным использованием.
    """

    def __init__(self, request_id: RequestId) -> None:
        self._request_id = request_id
        self._started = False
        self._thinking_started = False
        self._answer_started = False
        self._seen_tool_calls: set[int] = set()

    def reset(self) -> None:
        self._started = False
        self._thinking_started = False
        self._answer_started = False
        self._seen_tool_calls.clear()
    
    def set_request_id(self, request_id: RequestId) -> None:
        self._request_id = request_id

    def convert(self, stream: Iterator[ChatCompletionChunk]) -> Iterator[AgentEvent]:
        for chunk in stream:
            if not chunk.choices:
                continue
            choice = chunk.choices[0]
            delta = choice.delta

            if delta.role and not self._started:
                self._started = True
                yield GenerationStarted(
                    request_id=self._request_id,
                )

            extra = delta.model_extra or {}
            thinking = extra.get("reasoning_content") or extra.get("thinking")

            if thinking:
                if not self._thinking_started:
                    self._thinking_started = True
                    yield ThinkingStarted(
                        request_id=self._request_id,
                    )
                yield ThinkingToken(
                    request_id=self._request_id,
                    token=thinking,
                )

            if delta.content:
                if not self._answer_started:
                    self._answer_started = True
                    yield AnswerStarted(
                        request_id=self._request_id,
                    )
                yield AnswerToken(
                    request_id=self._request_id,
                    token=delta.content
                )

            if delta.refusal:
                yield RefusalToken(
                    request_id=self._request_id,
                    token=delta.refusal
                )

            if delta.tool_calls:
                for tc in delta.tool_calls:
                    if (
                        tc.index not in self._seen_tool_calls
                        and tc.id
                        and tc.function
                        and tc.function.name
                    ):
                        self._seen_tool_calls.add(tc.index)
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

            if choice.finish_reason:
                yield GenerationDone(
                    request_id=self._request_id,
                    finish_reason=choice.finish_reason,
                )


class OpenAIMiddleware(LLMMiddleware):
    """
    Terminal — вызывает OpenAI-совместимый API.
    Сам берёт messages из MessageService, строит запрос и стримит AgentEvent.
    """

    def __init__(self, config: LLMConfig, message_service: MessageService) -> None:
        self._client = OpenAI(base_url=config.base_url, api_key=config.api_key)
        self._message_service = message_service
        self._to_converter = ToOpenAIMessageConverter()

    def name(self) -> str:
        return "OpenAICompletion"

    def produce(self, ctx: AgentContext) -> Iterator[AgentEvent]:
        msg = self._message_service.message_iter()

        response = self._client.chat.completions.create(
            model=ctx.request.model,
            messages=self._to_converter.convert(msg),
            stream=True,
        )

        yield from FromOpenAIChunkConverter(ctx.request.request_id).convert(response)
