"""
Терминал LLM-слоя — обращение к OpenAI-совместимому API.
"""

from __future__ import annotations

import time
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from typing import Any

import httpx
import openai
from openai import OpenAI
from openai.types.chat.chat_completion_chunk import ChatCompletionChunk, Choice

from boba.adapters.llm.openai_errors import OpenAIErrorConverter
from boba.adapters.llm.openai_request import ToOpenAIRequestConverter
from boba.adapters.llm.openai_response import FromOpenAIChunkConverter
from boba.adapters.raw_llm_observer import RawLLMObserver, RequestOutcome
from boba.domain.config import LLMConfig
from boba.domain.core.patterns import StreamSource, StreamTransformer
from boba.domain.llm.errors import LLMError
from boba.domain.llm.events import (
    LLMEvent,
    LLMRequestSent,
    LLMRequestStarted,
    LLMUserPromptIssued,
)
from boba.domain.llm.models import LLMContext


def build_openai_client(config: LLMConfig) -> OpenAI:
    """
    Строит :class:`openai.OpenAI` из конфига
    """
    return OpenAI(base_url=config.base_url, api_key=config.api_key)


@contextmanager
def _observe_request(
    observer: RawLLMObserver, kwargs: dict[str, Any]
) -> Iterator[None]:
    """Оборачивает тело request-стрима парой ``on_request`` / ``on_request_end``.

    Классифицирует исход по типу исключения (или его отсутствию) и
    гарантирует единичный вызов ``on_request_end`` в любом случае —
    нормальное завершение, ``GeneratorExit`` от consumer-а,
    произвольное исключение из тела.
    """

    observer.on_request(kwargs)
    try:
        yield
    except GeneratorExit:
        observer.on_request_end(RequestOutcome.cancelled())
        raise
    except BaseException as e:
        observer.on_request_end(RequestOutcome.raised(e))
        raise
    else:
        observer.on_request_end(RequestOutcome.ok())


class OpenAITerminal(StreamSource[LLMContext, LLMEvent]):
    """
    Terminal LLM-слоя, вызывающий OpenAI-совместимый API.

    ``FromOpenAIChunkConverter`` держит внутри счетчики состояния.

    ``observer`` — наблюдатель сырых запросов/ответов. Вызывается
    до любой доменной конверсии: ``on_request(kwargs)`` перед HTTP-
    вызовом, ``on_response_chunk(chunk)`` на каждый входящий chunk,
    ``on_request_end(outcome, exception_name)`` при завершении стрима
    (:class:`RequestOutcome` ``OK``/``CANCELLED``/``RAISED``; имя
    класса исключения — только для ``RAISED``).

    ``preprocessor`` — pre-pipeline трансформер ``Choice → Choice``,
    выполняемый ДО fan-out в LLM-события. Обычно
    :class:`~boba.domain.core.patterns.StreamTransformerChain` из
    нескольких нормализаторов (reindexer коллизий ``index`` и т.п.).
    Перед каждым :meth:`stream` вызывается ``preprocessor.reset()`` —
    stateful-стадии получают чистое состояние per-request.
    """

    def __init__(
        self,
        client: OpenAI,
        observer: RawLLMObserver,
        preprocessor: StreamTransformer[LLMContext, Choice, Choice],
    ) -> None:
        self._client = client
        self._to_request = ToOpenAIRequestConverter()
        self._error_converter = OpenAIErrorConverter()
        self._observer = observer
        self._preprocessor = preprocessor

    def name(self) -> str:
        return "OpenAITerminal"

    def stream(self, ctx: LLMContext) -> Iterable[LLMEvent]:
        # превращаем :class:`LLMRequest` в аргументы вызова openai api
        # аргументов очень много и самый простой способ это собрать kwargs
        kwargs = self._to_request.convert(ctx.request)

        # observer-lifecycle вынесен в _observe_request: on_request до
        # любых yield, on_request_end в любом исходе (OK / GeneratorExit
        # от consumer-а / произвольное исключение).
        with _observe_request(self._observer, kwargs):
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

            self._preprocessor.reset()
            try:
                yield from FromOpenAIChunkConverter(
                    ctx.request_id, self._preprocessor
                ).stream(ctx, self._observe_chunks(response))
            except LLMError:
                raise
            except (openai.APIError, httpx.HTTPError) as e:
                raise self._error_converter.convert(e) from e

    def _observe_chunks(
        self, chunks: Iterable[ChatCompletionChunk]
    ) -> Iterable[ChatCompletionChunk]:
        for chunk in chunks:
            self._observer.on_response_chunk(chunk)
            yield chunk
