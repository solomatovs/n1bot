"""
Терминал LLM-слоя — обращение к OpenAI-совместимому API.
"""

from __future__ import annotations

import time
from collections.abc import Iterable

import httpx
import openai
from openai import OpenAI

from boba.domain.config import LLMConfig
from boba.domain.core.patterns import StreamSource
from boba_2.adapters.llm.openai_errors import OpenAIErrorConverter
from boba_2.adapters.llm.openai_request import ToOpenAIRequestConverter
from boba_2.adapters.llm.openai_response import FromOpenAIChunkConverter
from boba_2.domain.llm.errors import LLMError
from boba_2.domain.llm.events import (
    LLMEvent,
    LLMRequestSent,
    LLMRequestStarted,
    LLMUserPromptIssued,
)
from boba_2.domain.llm.models import LLMContext


def build_openai_client(config: LLMConfig) -> OpenAI:
    """
    Строит :class:`openai.OpenAI` из конфига
    """
    return OpenAI(base_url=config.base_url, api_key=config.api_key)


class OpenAITerminal(StreamSource[LLMContext, LLMEvent]):
    """
    Terminal LLM-слоя, вызывающий OpenAI-совместимый API.

    ``FromOpenAIChunkConverter`` держит внутри счетчики состояния
    """

    def __init__(self, client: OpenAI) -> None:
        self._client = client
        self._to_request = ToOpenAIRequestConverter()
        self._error_converter = OpenAIErrorConverter()

    def name(self) -> str:
        return "OpenAITerminal"

    def stream(self, ctx: LLMContext) -> Iterable[LLMEvent]:
        # превращаем :class:`LLMRequest` в аргументы вызова openai api
        # аргументов очень много и самый простой способ это собрать kwargs
        kwargs = self._to_request.convert(ctx.request)

        # snapshot user-prompt'а — что именно сейчас улетит в LLM
        yield LLMUserPromptIssued(
            request_id=ctx.request_id,
            user_prompt=ctx.request.user_message.content,
        )

        # парные события вокруг HTTP-вызова: Started/Sent
        # разница monotonic_ns даёт длительность провайдер-запроса
        # (сетевой round-trip + TTFB до получения stream-handle)
        yield LLMRequestStarted(
            request_id=ctx.request_id,
            model=ctx.request.model,
            messages_count=ctx.request.messages_count(),
            has_tools=ctx.request.has_tools(),
            monotonic_ns=time.monotonic_ns(),
        )

        # ошибка обработки запроса здесь классифицируется!
        # Terminal error - это всякие 401, 500, 502, которые нельзя повторить
        # Retryible error - это всякие лимиты по контексту
        try:
            response = self._client.chat.completions.create(**kwargs)
        except LLMError:
            raise
        except (openai.APIError, httpx.HTTPError) as e:
            raise self._error_converter.convert(e) from e

        yield LLMRequestSent(
            request_id=ctx.request_id,
            monotonic_ns=time.monotonic_ns(),
        )

        try:
            yield from FromOpenAIChunkConverter(ctx.request_id).stream(ctx, response)
        except LLMError:
            raise
        except (openai.APIError, httpx.HTTPError) as e:
            raise self._error_converter.convert(e) from e
