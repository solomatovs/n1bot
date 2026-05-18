"""LLMBuilder-extension для OpenAI-провайдера."""

from __future__ import annotations

from typing import Any

import httpx

import openai
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
    builder: LLMBuilder[
        dict[str, Any], ChatCompletionChunk, openai.APIError, httpx.HTTPError
    ],
    config: OpenAIConfig,
) -> LLMBuilder[
    dict[str, Any], ChatCompletionChunk, openai.APIError, httpx.HTTPError
]:
    """
    Подключить OpenAI-terminal к LLMBuilder
    """
    def terminal_factory(
        observer: LLMRequestObserver[
            dict[str, Any],
            ChatCompletionChunk,
            openai.APIError,
            httpx.HTTPError,
        ],
    ) -> StreamSource[LLMContext, LLMEvent]:
        return OpenAITerminal(
            build_openai_client(config, observer),
            observer=observer,
        )

    return builder.use_terminal(terminal_factory)
