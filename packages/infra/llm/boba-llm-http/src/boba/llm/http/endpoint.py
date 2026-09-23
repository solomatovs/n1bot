"""Общее для HTTP-провайдеров: секция с соединением и транспортом, маршруты
API, обмен json-телами через HttpTransport проекта и каркас чат-модели.

Провайдер не собирает URL и не трогает httpx: адрес, auth, ретраи и дамп
даёт HttpConnection с HttpTransportConfig, а сюда приходят только маршрут
и тело запроса. Ошибки транспорта переводятся в LlmError с адресом и
телом ответа. HttpChatModel ведёт обмен одинаково для любого wire-формата:
реализация даёт тело запроса, разбор строки и тела и склейку чанков.

Ошибки:
LlmError — endpoint недоступен, ответил статусом после ретраев, оборвал
    или задержал поток, либо ответил не по wire-контракту.
"""

from __future__ import annotations

import logging
from abc import abstractmethod
from collections.abc import AsyncIterator, Mapping, Sequence
from enum import StrEnum
from typing import Any, ClassVar, Generic, Protocol, TypeVar

import httpx
from pydantic import Field

from boba.llm.chat import (
    ChatDelta,
    ChatEvent,
    ChatModel,
    ChatReply,
    ChatRequest,
    LlmError,
    ToolSpec,
)
from boba.llm.providers import LlmProvider
from boba.toolkit.timing import Elapsed
from boba.transport.http import HttpRequest, HttpTransport, HttpTransportConfig
from boba.transport.http.connection import HttpConnection

__all__ = [
    "ChunkAssembly",
    "FunctionField",
    "HttpChatModel",
    "HttpLlmProvider",
    "LlmEndpoint",
    "LlmRoute",
    "WireDecoder",
]

logger = logging.getLogger(__name__)

C = TypeVar("C")
C_co = TypeVar("C_co", covariant=True)
C_contra = TypeVar("C_contra", contravariant=True)


class HttpLlmProvider(LlmProvider):
    """Секция удалённого провайдера: соединение с API и транспорт процесса."""

    connection: HttpConnection = Field(
        description=(
            "Адрес API частями (scheme/host/port/path) и auth; ключ провайдера — "
            "`auth = { method = 'bearer', token = '...' }`."
        ),
    )

    transport: HttpTransportConfig = Field(
        description=(
            "Поведение HTTP-транспорта процесса: таймауты, пул, дамп обмена; "
            'ссылкой `transport = "${http}"`.'
        ),
    )


class LlmRoute(StrEnum):
    """Маршруты API провайдеров относительно корня соединения."""

    CHAT_COMPLETIONS = "chat/completions"
    EMBEDDINGS = "embeddings"
    OLLAMA_CHAT = "api/chat"


class LlmEndpoint:
    """POST json на маршрут провайдера: тело целиком либо строками потока.

    Один экземпляр — один маршрут одного соединения; провайдер владеет
    wire-форматом, транспорт — сетью.
    """

    METHOD: ClassVar[str] = "POST"
    BODY_PREVIEW: ClassVar[int] = 500

    def __init__(
        self,
        transport: HttpTransport,
        connection: HttpConnection,
        route: LlmRoute,
        label: str,
    ) -> None:
        self._transport = transport
        self._route = route
        self._where = f"{label}: {self.METHOD} {connection.url_of(route.value)}"

    @property
    def where(self) -> str:
        """Метка провайдера и адрес запроса для текстов ошибок."""
        return self._where

    async def post(self, payload: Mapping[str, Any]) -> bytes:
        """Один запрос-ответ: тело ответа целиком, разбор — у провайдера."""
        try:
            async with self._transport.fetch(self._request(payload)) as response:
                return await response.stream.read()
        except httpx.HTTPStatusError as exc:
            raise self._status_error(exc) from exc
        except httpx.HTTPError as exc:
            msg = f"{self._where} failed: {type(exc).__name__}: {exc}"
            raise LlmError(msg) from exc

    async def stream(self, payload: Mapping[str, Any]) -> AsyncIterator[str]:
        """Строки потокового ответа; обрыв посреди потока — ошибка, не повтор."""
        try:
            async with self._transport.fetch(self._request(payload)) as response:
                async for line in response.stream.lines():
                    yield line
        except httpx.HTTPStatusError as exc:
            raise self._status_error(exc) from exc
        except httpx.HTTPError as exc:
            msg = f"{self._where}: stream failed: {type(exc).__name__}: {exc}"
            raise LlmError(msg) from exc

    def _request(self, payload: Mapping[str, Any]) -> HttpRequest:
        return HttpRequest(url=self._route.value, method=self.METHOD, json=payload)

    def _status_error(self, exc: httpx.HTTPStatusError) -> LlmError:
        body = exc.response.text[: self.BODY_PREVIEW]
        msg = f"{self._where} expected 2xx, got {exc.response.status_code}: {body!r}"

        return LlmError(msg)


class FunctionField(StrEnum):
    """Ключи объявления инструмента в формате function calling; его понимают
    и openai-совместимые серверы, и ollama."""

    TYPE = "type"
    FUNCTION = "function"
    NAME = "name"
    DESCRIPTION = "description"
    PARAMETERS = "parameters"


class WireDecoder(Protocol[C_co]):
    """Разбор строк потока в чанки wire-формата; экземпляр живёт один запрос."""

    @abstractmethod
    def feed(self, line: str) -> C_co | None: ...

    @abstractmethod
    def finish(self) -> C_co | None:
        """Недосланный чанк оборвавшегося потока; None — отдавать нечего."""
        ...


class ChunkAssembly(Protocol[C_contra]):
    """Склейка чанков wire-формата в события и финальное сообщение."""

    @abstractmethod
    def take(self, chunk: C_contra) -> ChatDelta | None: ...

    @abstractmethod
    def reply(self) -> ChatReply: ...


class HttpChatModel(ChatModel, Generic[C]):
    """Каркас чат-модели над LlmEndpoint: поток строк либо тело целиком,
    чанки в сборку, финал в журнал. Wire-формат — у наследника."""

    def __init__(self, endpoint: LlmEndpoint, model: str) -> None:
        self._endpoint = endpoint
        self._model = model

    @property
    def where(self) -> str:
        return self._endpoint.where

    @abstractmethod
    def _payload(self, request: ChatRequest) -> dict[str, Any]: ...

    @abstractmethod
    def _decoder(self) -> WireDecoder[C]: ...

    @abstractmethod
    def _parse_body(self, body: bytes) -> C: ...

    @abstractmethod
    def _assembly(self) -> ChunkAssembly[C]: ...

    async def chat(self, request: ChatRequest) -> AsyncIterator[ChatEvent]:
        payload = self._payload(request)

        elapsed = Elapsed()
        assembly = self._assembly()
        if request.stream:
            async for chunk in self._chunks(payload):
                emitted = assembly.take(chunk)
                if emitted is not None:
                    yield emitted
        else:
            body = await self._endpoint.post(payload)
            assembly.take(self._parse_body(body))

        reply = assembly.reply()
        logger.info(
            "%s: %s replied in %dms (%d call(s))",
            self._endpoint.where,
            self._model,
            elapsed.ms(),
            len(reply.tool_calls),
        )

        yield reply

    async def _chunks(self, payload: Mapping[str, Any]) -> AsyncIterator[C]:
        decoder = self._decoder()
        async for line in self._endpoint.stream(payload):
            chunk = decoder.feed(line)
            if chunk is None:
                continue

            yield chunk

        trailing = decoder.finish()
        if trailing is None:
            return

        yield trailing

    def _function_tools(self, tools: Sequence[ToolSpec]) -> list[dict[str, Any]]:
        wired: list[dict[str, Any]] = []
        for tool in tools:
            wired.append(
                {
                    FunctionField.TYPE.value: FunctionField.FUNCTION.value,
                    FunctionField.FUNCTION.value: {
                        FunctionField.NAME.value: tool.name,
                        FunctionField.DESCRIPTION.value: tool.description,
                        FunctionField.PARAMETERS.value: dict(tool.parameters),
                    },
                }
            )

        return wired
