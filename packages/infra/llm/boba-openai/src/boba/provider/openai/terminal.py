"""Терминал LLM-слоя — обращение к OpenAI-совместимому API."""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from typing import Any, cast

import httpx

import openai
from boba.llm.errors import LLMError, LLMProtocolError
from boba.llm.events import LLMEvent
from boba.llm.models import LLMContext
from boba.llm.observer import LLMRequestObserver
from boba.patterns import StreamSource
from boba.provider.openai.dto import OpenAIConfig
from boba.provider.openai.errors import OpenAIErrorConverter
from boba.provider.openai.request import ToOpenAIRequestConverter
from boba.provider.openai.response import (
    ChatCompletionChunkConsumer,
    ChatCompletionConsumer,
)
from openai import OpenAI
from openai.types.chat import ChatCompletion
from openai.types.chat.chat_completion_chunk import ChatCompletionChunk


def build_openai_client(
    config: OpenAIConfig,
    observer: LLMRequestObserver[
        dict[str, Any],
        ChatCompletionChunk,
        ChatCompletion,
        openai.APIError,
        httpx.HTTPError,
    ]
    | None = None,
) -> OpenAI:
    """Строит OpenAI-клиент из конфига; observer получает сырые HTTP req/resp."""
    if observer is None:
        return OpenAI(base_url=config.base_url, api_key=config.api_key)

    def _on_request(req: httpx.Request) -> None:
        observer.on_http_request(
            req.method,
            str(req.url),
            dict(req.headers),
            req.read(),
        )

    def _on_response(resp: httpx.Response) -> None:
        observer.on_http_response(resp.status_code, dict(resp.headers))

    http_client = httpx.Client(
        event_hooks={"request": [_on_request], "response": [_on_response]},
        verify=False,
    )
    return OpenAI(
        base_url=config.base_url,
        api_key=config.api_key,
        http_client=http_client,
    )


@contextmanager
def _observe_request(
    observer: LLMRequestObserver[
        dict[str, Any],
        ChatCompletionChunk,
        ChatCompletion,
        openai.APIError,
        httpx.HTTPError,
    ],
    kwargs: dict[str, Any],
) -> Iterator[None]:
    """Оборачивает тело стрима парой on_request / on_request_end|cancel.

    Исключительные исходы (api/http) фиксируются в catch-сайтах вызывающего кода
    через on_api_exception / on_http_exception — здесь не дублируются."""

    observer.on_request(kwargs)
    try:
        yield
    except GeneratorExit:
        observer.on_request_cancel()
        raise
    else:
        observer.on_request_end()


class OpenAITerminal(StreamSource[LLMContext, LLMEvent]):
    """Terminal LLM-слоя, вызывающий OpenAI-совместимый API."""

    def __init__(
        self,
        client: OpenAI,
        observer: LLMRequestObserver[
            dict[str, Any],
            ChatCompletionChunk,
            ChatCompletion,
            openai.APIError,
            httpx.HTTPError,
        ],
    ) -> None:
        self._client = client
        self._to_request = ToOpenAIRequestConverter()
        self._error_converter = OpenAIErrorConverter()
        self._observer = observer

    def name(self) -> str:
        return "OpenAITerminal"

    def stream(self, ctx: LLMContext) -> Iterable[LLMEvent]:
        kwargs = self._to_request.convert(ctx.request)

        with _observe_request(self._observer, kwargs):
            try:
                response = self._client.chat.completions.create(**kwargs)
            except LLMError:
                raise
            except openai.APIError as e:
                self._observer.on_api_exception(e)
                raise self._error_converter.convert(e) from e
            except httpx.HTTPError as e:
                self._observer.on_http_exception(e)
                raise self._error_converter.convert(e) from e

            try:
                request_id = ctx.request.request_id
                if ctx.request.stream:
                    # stream=True:
                    # ожидается Iterable[ChatCompletionChunk], если это не так
                    # значит ошибка декодирования потока
                    if not isinstance(response, Iterable):
                        raise LLMProtocolError(
                            "stream=True return not Iterable[ChatCompletionChunk]"
                        )

                    yield from ChatCompletionChunkConsumer(request_id).stream(
                        ctx, self._observe_chunks(response)
                    )
                else:
                    # stream=False:
                    #   один готовый ответ -> преобразуем в отдельные LLM-события
                    #   без Delta, но с Complited
                    completion = cast("ChatCompletion", response)
                    yield from ChatCompletionConsumer(request_id).consume(
                        self._observe_response(completion)
                    )
            except LLMError:
                raise
            except openai.APIError as e:
                self._observer.on_api_exception(e)
                raise self._error_converter.convert(e) from e
            except httpx.HTTPError as e:
                self._observer.on_http_exception(e)
                raise self._error_converter.convert(e) from e

    def _observe_chunks(
        self, chunks: Iterable[ChatCompletionChunk]
    ) -> Iterable[ChatCompletionChunk]:
        # перехват чанка для записи потока (stream=True)
        for chunk in chunks:
            self._observer.on_response_chunk(chunk)
            yield chunk

    def _observe_response(self, response: ChatCompletion) -> ChatCompletion:
        # перехват готового ответа для записи/метрик (stream=False)
        self._observer.on_response(response)
        return response
