"""OpenAI-совместимая реализация LLMCompletionService."""

from __future__ import annotations

import logging
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
from boba.domain.llm.llm import (
    LLMCompletionService,
    LLMDelta,
    LLMMessage,
    LLMRequest,
)

logger = logging.getLogger(__name__)


class OpenAICompletionService(LLMCompletionService):
    """
    Реализация LLMCompletionService через OpenAI-совместимый API.
    Работает с любым провайдером, поддерживающим OpenAI Chat Completions:
    OpenAI, Ollama, LM Studio, vLLM и т.д.
    """

    def __init__(self, config: LLMConfig) -> None:
        self._client = OpenAI(base_url=config.base_url, api_key=config.api_key)

    def stream(self, ctx: LLMRequest) -> Iterator[LLMDelta]:
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
