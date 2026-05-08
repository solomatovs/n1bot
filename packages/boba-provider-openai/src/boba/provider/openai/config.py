"""LLM-source-фабрика OpenAI-адаптера."""

from __future__ import annotations

from typing import Any

from boba.llm.events import LLMEvent
from boba.llm.models import LLMContext
from boba.llm.observer import LLMRequestObserver
from boba.patterns import (
    StreamSource,
    StreamSourceChainBuilder,
)
from boba.provider.openai.dto import OpenAIConfig
from boba.provider.openai.terminal import OpenAITerminal, build_openai_client
from openai.types.chat.chat_completion_chunk import ChatCompletionChunk


def create_llm_source(
    llm_config: OpenAIConfig,
    observer: LLMRequestObserver[dict[str, Any], ChatCompletionChunk],
    *,
    reindex_tool_calls: bool = True,
) -> StreamSource[LLMContext, LLMEvent]:
    """Готовый StreamSource поверх openai-SDK с подключённым observer'ом.

    reindex_tool_calls=False отключает починку коллизий index у параллельных
    tool_calls в стриме — для случаев, когда провайдер уже корректен.
    """
    return StreamSourceChainBuilder[LLMContext, LLMEvent]().terminal(
        OpenAITerminal(
            build_openai_client(llm_config, observer),
            observer=observer,
            reindex_tool_calls=reindex_tool_calls,
        )
    )
