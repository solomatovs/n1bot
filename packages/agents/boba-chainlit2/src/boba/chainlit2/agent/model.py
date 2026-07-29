from typing import ClassVar

from langchain_core.outputs import ChatGenerationChunk
from langchain_openai import ChatOpenAI


class ReasoningChatOpenAI(ChatOpenAI):
    """ChatOpenAI, сохраняющий reasoning_content OpenAI-совместимых провайдеров."""

    # поле-расширение (deepseek/openrouter/requesty/vllm); в официальной схеме
    # OpenAI его нет, поэтому langchain_openai его не разбирает и теряет
    REASONING_KEY: ClassVar[str] = "reasoning_content"

    def _convert_chunk_to_generation_chunk(
        self,
        chunk: dict,
        default_chunk_class: type,
        base_generation_info: dict | None,
    ) -> ChatGenerationChunk | None:
        generation_chunk = super()._convert_chunk_to_generation_chunk(
            chunk, default_chunk_class, base_generation_info
        )
        if generation_chunk is None:
            return None

        choices = chunk.get("choices") or chunk.get("chunk", {}).get("choices") or []
        if not choices:
            return generation_chunk

        delta = choices[0].get("delta") or {}
        if reasoning := delta.get(self.REASONING_KEY):
            # сложение чанков сливает additional_kwargs через merge_dicts,
            # строки при этом конкатенируются — накапливать вручную не нужно
            generation_chunk.message.additional_kwargs[self.REASONING_KEY] = reasoning

        return generation_chunk
