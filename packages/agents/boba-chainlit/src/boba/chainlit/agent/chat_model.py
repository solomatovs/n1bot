"""ChatOpenAI, сохраняющий рассуждения провайдера: штатный класс их выбрасывает."""

from __future__ import annotations

from enum import StrEnum
from typing import Any, ClassVar

from langchain_core.outputs import ChatGenerationChunk, ChatResult
from langchain_openai import ChatOpenAI
from typing_extensions import override

__all__ = ["ReasoningChatOpenAI", "ResponseField"]


class ResponseField(StrEnum):
    """Имена полей в ответе OpenAI-совместимого провайдера."""

    CHOICES = "choices"
    DELTA = "delta"
    MESSAGE = "message"
    REASONING_CONTENT = "reasoning_content"
    REASONING = "reasoning"


class ReasoningChatOpenAI(ChatOpenAI):
    """ChatOpenAI, нормализующий рассуждения провайдера в reasoning_content."""

    _REASONING_FIELDS: ClassVar[tuple[ResponseField, ...]] = (
        ResponseField.REASONING_CONTENT,
        ResponseField.REASONING,
    )

    @staticmethod
    def _reasoning_of(payload: dict) -> str:
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
        choices = chunk.get(ResponseField.CHOICES.value) or []
        if choices:
            delta = choices[0].get(ResponseField.DELTA.value) or {}
            reasoning = self._reasoning_of(delta)
            if reasoning:
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
        choices = response_dict.get(ResponseField.CHOICES.value) or []
        if choices and result.generations:
            message = choices[0].get(ResponseField.MESSAGE.value) or {}
            reasoning = self._reasoning_of(message)
            if reasoning:
                result.generations[0].message.additional_kwargs[
                    ResponseField.REASONING_CONTENT.value
                ] = reasoning
        return result
