"""Общее для HTTP-провайдеров: секция с соединением и транспортом, маршруты
API, обмен json-телами через HttpTransport проекта и каркас чат-модели.

Провайдер не собирает URL и не трогает httpx: адрес, auth, ретраи и дамп
даёт HttpConnection с HttpTransportConfig, а сюда приходят только маршрут
и тело запроса. Ошибки транспорта переводятся в LlmError с адресом и
телом ответа. HttpChatModel ведёт обмен одинаково для любого wire-формата:
реализация даёт тело запроса, разбор строки и тела и склейку чанков.

Ошибки:
LlmContextOverflowError — 4xx с текстом о длине контекста: запрос не поместился в окно.
LlmServiceError — endpoint недоступен или ответил статусом после ретраев: вид
    беды и короткая причина (статус и error.message ответа) для человека.
LlmError — endpoint оборвал или задержал поток либо ответил не по
    wire-контракту.
"""

from __future__ import annotations

import json
import logging
from abc import abstractmethod
from collections.abc import AsyncIterator, Mapping, Sequence
from enum import StrEnum
from http import HTTPStatus
from typing import Any, ClassVar, Generic, Protocol, TypeVar

from pydantic import Field

from boba.llm.chat import (
    ChatDelta,
    ChatEvent,
    ChatModel,
    ChatReply,
    ChatRequest,
    ContextOverflowSigns,
    LlmContextOverflowError,
    LlmError,
    LlmServiceError,
    LlmTrouble,
    ToolSpec,
)
from boba.llm.providers import LlmProvider
from boba.toolkit.timing import Elapsed
from boba.transport.http import (
    HttpRequest,
    HttpStatusError,
    HttpTransport,
    HttpTransportConfig,
    TransportError,
)
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


class ErrorBodyKey(StrEnum):
    """Стандартные поля тела отказа: error.message OpenAI и строка error ollama."""

    ERROR = "error"
    MESSAGE = "message"


class ProviderRefusal:
    """Отказ провайдера словами для человека: статус и суть из стандартного
    поля ответа — error.message (OpenAI и совместимые) или строки error
    (ollama); тело другого вида в текст не идёт, оно остаётся в журнале.
    Держит его в поле LlmEndpoint."""

    LIMIT: ClassVar[int] = 200

    def detail(self, status: int, body: str) -> str:
        message = self._message(body)
        phrase = self._phrase(status)
        if not message:
            return phrase

        return f"{phrase}: {message[: self.LIMIT]}"

    def trouble(self, status: int) -> LlmTrouble:
        if status == HTTPStatus.TOO_MANY_REQUESTS:
            return LlmTrouble.BUSY

        if status >= HTTPStatus.INTERNAL_SERVER_ERROR:
            return LlmTrouble.DOWN

        return LlmTrouble.REJECTED

    @staticmethod
    def _phrase(status: int) -> str:
        try:
            return f"HTTP {status} {HTTPStatus(status).phrase}"
        except ValueError:
            return f"HTTP {status}"

    def _message(self, body: str) -> str:
        try:
            document = json.loads(body)
        except json.JSONDecodeError:
            return ""

        if not isinstance(document, dict):
            return ""

        error = document.get(ErrorBodyKey.ERROR.value)
        if isinstance(error, str):
            return error

        if not isinstance(error, dict):
            return ""

        message = error.get(ErrorBodyKey.MESSAGE.value)
        if isinstance(message, str):
            return message

        return ""


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

    def __init__(
        self,
        transport: HttpTransport,
        connection: HttpConnection,
        route: LlmRoute,
        label: str,
    ) -> None:
        self._transport = transport
        self._route = route
        self._label = label
        self._where = f"{label}: {self.METHOD} {connection.url_of(route.value)}"
        self._overflow = ContextOverflowSigns()
        self._refusal = ProviderRefusal()

    @property
    def where(self) -> str:
        """Метка провайдера и адрес запроса для текстов ошибок."""
        return self._where

    async def post(self, payload: Mapping[str, Any]) -> bytes:
        """Один запрос-ответ: тело ответа целиком, разбор — у провайдера."""
        try:
            async with self._transport.fetch(self._request(payload)) as response:
                return await response.stream.read()
        except HttpStatusError as exc:
            raise self._status_error(exc, f"{self._label}: {exc}") from exc
        except TransportError as exc:
            raise self._unreachable(exc, f"{self._label}: {exc}") from exc

    async def stream(self, payload: Mapping[str, Any]) -> AsyncIterator[str]:
        """Строки потокового ответа; обрыв посреди потока — ошибка, не повтор."""
        try:
            async with self._transport.fetch(self._request(payload)) as response:
                async for line in response.stream.lines():
                    yield line
        except HttpStatusError as exc:
            message = f"{self._label}: stream failed: {exc}"
            raise self._status_error(exc, message) from exc
        except TransportError as exc:
            message = f"{self._label}: stream failed: {exc}"
            raise self._unreachable(exc, message) from exc

    def _status_error(self, exc: HttpStatusError, message: str) -> LlmError:
        """4xx с текстом о длине контекста — переполнение окна, остальное —
        LlmServiceError с видом беды по статусу."""
        trouble = self._refusal.trouble(exc.status)
        refused = LlmServiceError(
            message, trouble, self._refusal.detail(exc.status, exc.body)
        )
        if trouble is not LlmTrouble.REJECTED:
            return refused

        if self._overflow.matches(exc.body):
            return LlmContextOverflowError(message)

        return refused

    def _unreachable(self, exc: TransportError, message: str) -> LlmServiceError:
        return LlmServiceError(message, LlmTrouble.DOWN, exc.failure.value)

    def _request(self, payload: Mapping[str, Any]) -> HttpRequest:
        return HttpRequest(url=self._route.value, method=self.METHOD, json=payload)


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
    def reply(self, streamed: bool) -> ChatReply:
        """Финал; у потока без финального чанка — LlmError, тело целиком
        финала не требует."""


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

        reply = assembly.reply(streamed=request.stream)
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
        """Инструменты функциями в порядке списка. Загруженный отложенный
        (ToolSpec.deferred) идёт обычной записью: родных отложенных
        инструментов у протокола нет, а вызывающий ставит такие в конец."""
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
