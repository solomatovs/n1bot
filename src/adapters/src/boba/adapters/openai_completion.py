"""OpenAI-совместимая реализация LLMCompletionService."""

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
from boba.domain.agent.llm import LLMCompletionService
from boba.domain.agent.models import LLMMessage, LLMRequest
from boba.domain.agent.events import (
    AgentEvent,
    AnswerToken,
    GenerationDone,
    GenerationStarted,
    RefusalToken,
    ThinkingToken,
    ToolCallArgumentDelta,
    ToolCallBegin,
)
from boba.domain.core.stream import ActiveConverter, PassiveConverter

logger = logging.getLogger(__name__)


class LoggingLLMMiddleware(LLMCompletionService):
    """Логирует запрос, количество чанков и время генерации."""

    def __init__(self, next: LLMCompletionService) -> None:
        self._next = next

    def name(self) -> str:
        return "LoggingLLM"

    def produce(self, ctx: LLMRequest) -> Iterator[AgentEvent]:
        logger.info("LLM request: model=%s", ctx.model)
        start = time.monotonic()
        chunks = 0

        for event in self._next.produce(ctx):
            chunks += 1
            yield event

        elapsed = time.monotonic() - start
        logger.info("LLM done: %d chunks in %.2fs", chunks, elapsed)


class StupedRetryLLMMiddleware(LLMCompletionService):
    """Повторяет запрос при ошибке до max_retries раз."""

    def __init__(self, next: LLMCompletionService, max_retries: int = 3) -> None:
        self._next = next
        self._max_retries = max_retries

    def name(self) -> str:
        return "RetryLLM"

    def produce(self, ctx: LLMRequest) -> Iterator[AgentEvent]:
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


class OpenAIMessagePassiveConverter(
    PassiveConverter[LLMMessage, ChatCompletionMessageParam]
):
    def convert(self, item: LLMMessage) -> ChatCompletionMessageParam:
        match item.role:
            case "system":
                return ChatCompletionSystemMessageParam(
                    role="system",
                    content=item.content,
                )
            case "user":
                return ChatCompletionUserMessageParam(
                    role="user",
                    content=item.content,
                )
            case "assistant":
                param = ChatCompletionAssistantMessageParam(
                    role="assistant",
                    content=item.content,
                )
                if item.tool_calls:
                    param["tool_calls"] = [
                        {
                            "id": tc.id,
                            "type": "function",
                            "function": {"name": tc.name, "arguments": tc.arguments},
                        }
                        for tc in item.tool_calls
                    ]
                return param
            case "tool":
                return ChatCompletionToolMessageParam(
                    role="tool",
                    content=item.content,
                    tool_call_id=item.tool_call_id or "",
                )
            case _:
                raise ValueError(f"Unknown message role: {item.role}")


class OpenAIMessageConverter(ActiveConverter[LLMMessage, ChatCompletionMessageParam]):
    """Конвертирует поток LLMMessage в поток ChatCompletionMessageParam."""

    def __init__(self) -> None:
        super().__init__()
        self._inner = OpenAIMessagePassiveConverter()

    def convert(self, items: Iterator[LLMMessage]) -> Iterator[ChatCompletionMessageParam]:
        for msg in items:
            yield self._inner.convert(msg)


class OpenAICompletionService(LLMCompletionService):
    """
    Реализация LLMCompletionService через OpenAI-совместимый API.
    Работает с любым провайдером, поддерживающим OpenAI Chat Completions:
    OpenAI, Ollama, LM Studio, vLLM и т.д.

    Yield-ит AgentEvent напрямую (delta converter встроен).
    """

    def __init__(self, config: LLMConfig) -> None:
        self._client = OpenAI(base_url=config.base_url, api_key=config.api_key)
        self._converter = OpenAIMessageConverter()

    def name(self) -> str:
        return "OpenAICompletion"

    def produce(self, ctx: LLMRequest) -> Iterator[AgentEvent]:
        response = self._client.chat.completions.create(
            model=ctx.model,
            messages=self._converter.convert(ctx.messages),
            stream=True,
        )

        started = False
        seen_tool_calls: set[int] = set()

        for chunk in response:
            if not chunk.choices:
                continue
            choice = chunk.choices[0]
            delta = choice.delta

            # Первый chunk с role — генерация началась
            if delta.role and not started:
                started = True
                yield GenerationStarted()

            # thinking/reasoning из model_extra (DeepSeek, Qwen)
            extra = delta.model_extra or {}
            thinking = extra.get("reasoning_content") or extra.get("thinking")

            if thinking:
                yield ThinkingToken(token=thinking)

            if delta.content:
                yield AnswerToken(token=delta.content)

            if delta.refusal:
                yield RefusalToken(token=delta.refusal)

            # Tool call deltas
            if delta.tool_calls:
                for tc in delta.tool_calls:
                    if tc.index not in seen_tool_calls and tc.id and tc.function and tc.function.name:
                        seen_tool_calls.add(tc.index)
                        yield ToolCallBegin(
                            index=tc.index,
                            tool_call_id=tc.id,
                            tool_name=tc.function.name,
                        )
                    if tc.function and tc.function.arguments:
                        yield ToolCallArgumentDelta(
                            index=tc.index,
                            arguments=tc.function.arguments,
                        )

            # Finish reason
            if choice.finish_reason:
                yield GenerationDone(finish_reason=choice.finish_reason)
