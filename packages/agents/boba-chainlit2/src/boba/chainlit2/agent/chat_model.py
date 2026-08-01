"""ReasoningChatOpenAI: ChatOpenAI + перенос reasoning_content из ответа.

langchain-openai намеренно выбрасывает нестандартные поля провайдеров
(см. docstring пакета: «Non-standard response fields added by third-party
providers (e.g., reasoning_content) are not extracted or preserved»).
DeepSeek-совместимые провайдеры (requesty, qwen, deepseek, openrouter)
отдают рассуждения модели в choices[].delta.reasoning_content (стрим) и
choices[].message.reasoning_content (не-стрим) — подтверждено дампами
requesty (DumpingTransport).

Сабкласс достаёт поле из сырого ответа и кладёт в
additional_kwargs["reasoning_content"]. При стриме langchain мержит
additional_kwargs чанков конкатенацией строк, поэтому в on_llm_end
агрегированное message несёт полный текст рассуждений — AgentTracer
показывает его шагом «llm thinking».
"""

from __future__ import annotations

from typing import Any

from langchain_core.outputs import ChatGenerationChunk, ChatResult
from langchain_openai import ChatOpenAI
from typing_extensions import override

__all__ = ["ReasoningChatOpenAI"]


class ReasoningChatOpenAI(ChatOpenAI):
    """ChatOpenAI, сохраняющий reasoning_content провайдера."""

    @override
    def _convert_chunk_to_generation_chunk(
        self,
        chunk: dict,
        default_chunk_class: type,
        base_generation_info: dict | None,
    ) -> ChatGenerationChunk | None:
        generation_chunk = super()._convert_chunk_to_generation_chunk(
            chunk, default_chunk_class, base_generation_info,
        )
        if generation_chunk is None:
            return None
        choices = chunk.get("choices") or []
        if choices:
            delta = choices[0].get("delta") or {}
            reasoning = delta.get("reasoning_content")
            if reasoning:
                generation_chunk.message.additional_kwargs["reasoning_content"] = (
                    str(reasoning)
                )
        return generation_chunk

    @override
    def _create_chat_result(
        self,
        response: Any,
        generation_info: dict | None = None,
    ) -> ChatResult:
        result = super()._create_chat_result(response, generation_info)
        response_dict = (
            response if isinstance(response, dict) else response.model_dump()
        )
        choices = response_dict.get("choices") or []
        if choices and result.generations:
            message = choices[0].get("message") or {}
            reasoning = message.get("reasoning_content")
            if reasoning:
                result.generations[0].message.additional_kwargs[
                    "reasoning_content"
                ] = str(reasoning)
        return result
