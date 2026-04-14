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
from boba.domain.llm.llm import LLMCompletionService, LLMDelta, LLMMessage, LLMRequest


logger = logging.getLogger(__name__)


class LoggingLLMMiddleware(LLMCompletionService):
    """Логирует запрос, количество чанков и время генерации."""

    def __init__(self, next: LLMCompletionService) -> None:
        self._next = next

    def name(self) -> str:
        return "LoggingLLM"

    def produce(self, ctx: LLMRequest) -> Iterator[LLMDelta]:
        logger.info("LLM request: model=%s", ctx.model)
        start = time.monotonic()
        chunks = 0

        for delta in self._next.produce(ctx):
            chunks += 1
            yield delta

        elapsed = time.monotonic() - start
        logger.info("LLM done: %d chunks in %.2fs", chunks, elapsed)


class StupedRetryLLMMiddleware(LLMCompletionService):
    """Повторяет запрос при ошибке до max_retries раз."""

    def __init__(self, next: LLMCompletionService, max_retries: int = 3) -> None:
        self._next = next
        self._max_retries = max_retries

    def name(self) -> str:
        return "RetryLLM"

    def produce(self, ctx: LLMRequest) -> Iterator[LLMDelta]:
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


class OpenAICompletionService(LLMCompletionService):
    """
    Реализация LLMCompletionService через OpenAI-совместимый API.
    Работает с любым провайдером, поддерживающим OpenAI Chat Completions:
    OpenAI, Ollama, LM Studio, vLLM и т.д.
    """

    def __init__(self, config: LLMConfig) -> None:
        self._client = OpenAI(base_url=config.base_url, api_key=config.api_key)

    def name(self) -> str:
        return "OpenAICompletion"

    def produce(self, ctx: LLMRequest) -> Iterator[LLMDelta]:
        response = self._client.chat.completions.create(
            model=ctx.model,
            messages=map(self._to_openai_message, ctx.messages),
            stream=True,
        )

        for chunk in response:
            delta = chunk.choices[0].delta

            yield LLMDelta(thinking=None, content=delta.content)

    @staticmethod
    def _to_openai_message(msg: LLMMessage) -> ChatCompletionMessageParam:
        """Конвертирует LLMMessage в формат OpenAI API."""
        match msg.role:
            case "system":
                return ChatCompletionSystemMessageParam(
                    role="system", content=msg.content,
                )
            case "user":
                return ChatCompletionUserMessageParam(
                    role="user", content=msg.content,
                )
            case "assistant":
                param = ChatCompletionAssistantMessageParam(
                    role="assistant", content=msg.content,
                )
                if msg.tool_calls:
                    param["tool_calls"] = [
                        {
                            "id": tc.id,
                            "type": "function",
                            "function": {"name": tc.name, "arguments": tc.arguments},
                        }
                        for tc in msg.tool_calls
                    ]
                return param
            case "tool":
                return ChatCompletionToolMessageParam(
                    role="tool",
                    content=msg.content,
                    tool_call_id=msg.tool_call_id or "",
                )
            case _:
                raise ValueError(f"Unknown message role: {msg.role}")
