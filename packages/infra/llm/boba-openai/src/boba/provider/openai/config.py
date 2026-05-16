"""LLMBuilder-extension для OpenAI-провайдера."""

from __future__ import annotations

from typing import Any

from boba.llm.builder import LLMBuilder
from boba.llm.events import LLMEvent
from boba.llm.models import LLMContext
from boba.llm.observer import LLMRequestObserver
from boba.patterns import StreamSource
from boba.provider.openai.dto import OpenAIConfig
from boba.provider.openai.terminal import OpenAITerminal, build_openai_client
from openai.types.chat.chat_completion_chunk import ChatCompletionChunk

__all__ = ["use_openai"]


def use_openai(
    builder: LLMBuilder[dict[str, Any], ChatCompletionChunk],
    config: OpenAIConfig,
    *,
    reindex_tool_calls: bool = True,
) -> LLMBuilder[dict[str, Any], ChatCompletionChunk]:
    """Подключить OpenAI-terminal к LLMBuilder.

    reindex_tool_calls=False отключает починку коллизий index у параллельных
    tool_calls в стриме — для случаев, когда провайдер уже корректен.
    """
    def terminal_factory(
        observer: LLMRequestObserver[dict[str, Any], ChatCompletionChunk],
    ) -> StreamSource[LLMContext, LLMEvent]:
        return OpenAITerminal(
            build_openai_client(config, observer),
            observer=observer,
            reindex_tool_calls=reindex_tool_calls,
        )

    return builder.use_terminal(terminal_factory)
