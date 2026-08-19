"""ChatOpenAI, сохраняющий рассуждения провайдера: штатный класс их выбрасывает."""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from typing import Any, ClassVar

from langchain_core.messages import BaseMessage
from langchain_core.outputs import (
    ChatGeneration,
    ChatGenerationChunk,
    ChatResult,
    GenerationChunk,
    LLMResult,
)
from langchain_openai import ChatOpenAI
from typing_extensions import override

__all__ = [
    "GeneratedMessage",
    "ReasoningChatOpenAI",
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


class ReasoningChatOpenAI(ChatOpenAI):
    """ChatOpenAI, нормализующий рассуждения провайдера в reasoning_content.

    Рассуждения ходят в обе стороны: провайдеры в режиме размышления требуют
    вернуть reasoning_content вместе с сообщением ассистента, а штатный класс
    отправляет обратно только текст и вызовы инструментов.
    """

    _REASONING_FIELDS: ClassVar[tuple[ResponseField, ...]] = (
        ResponseField.REASONING_CONTENT,
        ResponseField.REASONING,
    )

    ASSISTANT_ROLE: ClassVar[str] = "assistant"

    @override
    def _get_request_payload(
        self,
        input_: Any,
        *,
        stop: list[str] | None = None,
        **kwargs: Any,
    ) -> dict:
        payload = super()._get_request_payload(input_, stop=stop, **kwargs)

        messages = payload.get("messages")
        if not isinstance(messages, list):
            return payload

        sources = self._convert_input(input_).to_messages()
        if len(sources) != len(messages):
            return payload

        for source, target in zip(sources, messages, strict=True):
            self._restore_reasoning(source, target)

        return payload

    @classmethod
    def _restore_reasoning(cls, source: BaseMessage, target: Any) -> None:
        """Возвращает рассуждения в сообщение запроса; чужие роли не трогает."""
        if not isinstance(target, dict):
            return

        if target.get("role") != cls.ASSISTANT_ROLE:
            return

        field = ResponseField.REASONING_CONTENT.value
        if field in target:
            return

        if field not in source.additional_kwargs:
            return

        target[field] = ReasoningText.of(source)

    @staticmethod
    def _reasoning_of(payload: Mapping[str, Any]) -> str:
        for field in ReasoningChatOpenAI._REASONING_FIELDS:
            value = payload.get(field.value)
            if value:
                return str(value)

        return ""

    @override
    def _convert_chunk_to_generation_chunk(
        self,
        chunk: dict,
        default_chunk_class: type,
        base_generation_info: dict | None,
    ) -> ChatGenerationChunk | None:
        generation_chunk = super()._convert_chunk_to_generation_chunk(
            chunk,
            default_chunk_class,
            base_generation_info,
        )
        if generation_chunk is None:
            return None

        choices = chunk.get(ResponseField.CHOICES.value)
        if not choices:
            return generation_chunk

        delta = choices[0].get(ResponseField.DELTA.value)
        if not delta:
            return generation_chunk

        reasoning = self._reasoning_of(delta)
        if not reasoning:
            return generation_chunk

        generation_chunk.message.additional_kwargs[
            ResponseField.REASONING_CONTENT.value
        ] = reasoning

        return generation_chunk

    @override
    def _create_chat_result(
        self,
        response: Any,
        generation_info: dict | None = None,
    ) -> ChatResult:
        result = super()._create_chat_result(response, generation_info)
        if isinstance(response, dict):
            response_dict = response
        else:
            response_dict = response.model_dump()

        if not result.generations:
            return result

        choices = response_dict.get(ResponseField.CHOICES.value)
        if not choices:
            return result

        message = choices[0].get(ResponseField.MESSAGE.value)
        if not message:
            return result

        reasoning = self._reasoning_of(message)
        if not reasoning:
            return result

        result.generations[0].message.additional_kwargs[
            ResponseField.REASONING_CONTENT.value
        ] = reasoning

        return result
