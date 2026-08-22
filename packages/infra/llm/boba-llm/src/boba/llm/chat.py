"""Помощники разбора ответов модели: имена полей и извлечение рассуждений.

Чат-модель хода собирается стандартом провайдеров (boba.llm.provider) и
мостом ProviderChatModel (boba.llm.bridge); здесь остаются только помощники,
которыми лента и трасер читают сообщения langchain.

Ошибки: своих не выпускает.
"""

from __future__ import annotations

from enum import StrEnum

from langchain_core.messages import BaseMessage
from langchain_core.outputs import (
    ChatGeneration,
    ChatGenerationChunk,
    GenerationChunk,
    LLMResult,
)

__all__ = [
    "GeneratedMessage",
    "ReasoningText",
    "ResponseField",
]


class ResponseField(StrEnum):
    """Имена полей в ответе OpenAI-совместимого провайдера."""

    CHOICES = "choices"
    DELTA = "delta"
    MESSAGE = "message"
    REASONING_CONTENT = "reasoning_content"
    REASONING = "reasoning"


class GeneratedMessage:
    """Сообщение langchain из результата генерации; у текстовых чанков его нет."""

    @staticmethod
    def of_chunk(
        chunk: GenerationChunk | ChatGenerationChunk | None,
    ) -> BaseMessage | None:
        if not isinstance(chunk, ChatGenerationChunk):
            return None

        return chunk.message

    @staticmethod
    def of_result(response: LLMResult) -> BaseMessage | None:
        if not response.generations:
            return None

        first = response.generations[0]
        if not first:
            return None

        generation = first[0]
        if not isinstance(generation, ChatGeneration):
            return None

        return generation.message


class ReasoningText:
    """Рассуждения сообщения: провайдер нормализует их в additional_kwargs."""

    @staticmethod
    def of(message: BaseMessage | None) -> str:
        if message is None:
            return ""

        value = message.additional_kwargs.get(ResponseField.REASONING_CONTENT.value)
        if not value:
            return ""

        return str(value)
