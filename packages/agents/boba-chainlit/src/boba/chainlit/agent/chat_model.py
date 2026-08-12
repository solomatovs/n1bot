"""ChatOpenAI, сохраняющий рассуждения провайдера: штатный класс их выбрасывает."""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from typing import Any, ClassVar

from langchain_core.outputs import ChatGenerationChunk, ChatResult
from langchain_openai import ChatOpenAI
from typing_extensions import override

__all__ = ["ReasoningChatOpenAI", "ReasoningText", "ResponseField"]


class ResponseField(StrEnum):
    """Имена полей в ответе OpenAI-совместимого провайдера."""

    CHOICES = "choices"
    DELTA = "delta"
    MESSAGE = "message"
    REASONING_CONTENT = "reasoning_content"
    REASONING = "reasoning"


class ReasoningText:
    """Рассуждения из сообщения langchain: своё поле либо additional_kwargs."""

    @staticmethod
    def of(message: Any) -> str:
        if message is None:
            return ""

        value = getattr(message, ResponseField.REASONING_CONTENT.value, None)
        if value:
            return str(value)

        extra = getattr(message, "additional_kwargs", None)
        if not extra:
            return ""

        value = extra.get(ResponseField.REASONING_CONTENT.value)
        if value:
            return str(value)

        return ""


class ReasoningChatOpenAI(ChatOpenAI):
    """ChatOpenAI, нормализующий рассуждения провайдера в reasoning_content."""

    _REASONING_FIELDS: ClassVar[tuple[ResponseField, ...]] = (
        ResponseField.REASONING_CONTENT,
        ResponseField.REASONING,
    )

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
