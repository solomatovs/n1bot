"""Нативный чат-бэкенд ollama: /api/chat потоком NDJSON.

Провайдер владеет wire-форматом целиком: сборка тела запроса (сообщения с
thinking и tool_calls-объектами, таблица sampling как есть, options с
переложенным из моста stop), построчный разбор NDJSON-чанков, вотчдог пауз
между чанками и повторы до первого полученного байта.

Ошибки:
ChatProviderError — endpoint недоступен, ответил статусом, мусором или
    чанком-ошибкой, либо пауза между чанками превысила потолок конфига.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Mapping, Sequence
from enum import StrEnum
from typing import Any, ClassVar
from uuid import uuid4

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from boba.chat.provider import (
    ChatDelta,
    ChatEvent,
    ChatProvider,
    ChatProviderError,
    ChatReply,
    ChatRequest,
    ChatRole,
    ChatTurn,
    ChatUsage,
    OllamaChatConfig,
    ToolCallRequest,
    ToolSpec,
)
from boba.toolkit.timing import Elapsed

logger = logging.getLogger(__name__)

__all__ = ["OllamaChatProvider"]


class OllamaField(StrEnum):
    """Ключи wire-формата /api/chat."""

    MODEL = "model"
    MESSAGES = "messages"
    ROLE = "role"
    CONTENT = "content"
    THINKING = "thinking"
    TOOLS = "tools"
    TOOL_CALLS = "tool_calls"
    TOOL_CALL_ID = "tool_call_id"
    TYPE = "type"
    FUNCTION = "function"
    NAME = "name"
    DESCRIPTION = "description"
    PARAMETERS = "parameters"
    ARGUMENTS = "arguments"
    ID = "id"
    STREAM = "stream"
    STOP = "stop"
    OPTIONS = "options"


class OllamaWireFunction(BaseModel):
    """function вызова инструмента; аргументы приходят объектом."""

    model_config = ConfigDict(extra="ignore")

    name: str = ""
    arguments: Mapping[str, Any] = Field(default_factory=dict)


class OllamaWireCall(BaseModel):
    """Вызов инструмента чанка; приходит целиком, не дельтами."""

    model_config = ConfigDict(extra="ignore")

    id: str = ""
    function: OllamaWireFunction = OllamaWireFunction()


class OllamaWireMessage(BaseModel):
    """message одного чанка."""

    model_config = ConfigDict(extra="ignore")

    content: str = ""
    thinking: str = ""
    tool_calls: Sequence[OllamaWireCall] = ()


class OllamaWireChunk(BaseModel):
    """Одна NDJSON-строка потока или всё тело нестримящего ответа."""

    model_config = ConfigDict(extra="ignore")

    message: OllamaWireMessage = OllamaWireMessage()
    done: bool = False
    prompt_eval_count: int = 0
    eval_count: int = 0
    error: str = ""


class OllamaAssembly:
    """Склейка потока чанков в финальное сообщение."""

    def __init__(self) -> None:
        self._content: list[str] = []
        self._thinking: list[str] = []
        self._calls: list[ToolCallRequest] = []
        self._input_tokens = 0
        self._output_tokens = 0

    def take(self, chunk: OllamaWireChunk) -> ChatDelta | None:
        """Учитывает чанк; наружу — прирост текста или рассуждений."""
        if chunk.error:
            msg = f"chat endpoint reported: {chunk.error}"
            raise ChatProviderError(msg)

        if chunk.done:
            self._input_tokens = chunk.prompt_eval_count
            self._output_tokens = chunk.eval_count

        for call in chunk.message.tool_calls:
            self._calls.append(self._call(call))

        message = chunk.message
        if message.thinking:
            self._thinking.append(message.thinking)

        if message.content:
            self._content.append(message.content)

        if not message.content and not message.thinking:
            return None

        return ChatDelta(content=message.content, reasoning=message.thinking)

    def reply(self) -> ChatReply:
        return ChatReply(
            content="".join(self._content),
            reasoning="".join(self._thinking),
            tool_calls=list(self._calls),
            usage=ChatUsage(
                input_tokens=self._input_tokens,
                output_tokens=self._output_tokens,
            ),
        )

    @staticmethod
    def _call(call: OllamaWireCall) -> ToolCallRequest:
        """Вызов конверта; без id от сервера вызов получает локальный uuid."""
        call_id = call.id
        if not call_id:
            call_id = uuid4().hex

        return ToolCallRequest(
            id=call_id,
            name=call.function.name,
            arguments=dict(call.function.arguments),
        )


class OllamaChatProvider(ChatProvider):
    """ChatProvider поверх нативного endpoint'а ollama."""

    ENDPOINT: ClassVar[str] = "api/chat"

    RETRY_STATUSES: ClassVar[frozenset[int]] = frozenset({429, 500, 502, 503, 504})

    def __init__(
        self,
        cfg: OllamaChatConfig,
        client: httpx.AsyncClient,
        model: str,
    ) -> None:
        self._cfg = cfg
        self._client = client
        self._model = model

    async def chat(self, request: ChatRequest) -> AsyncIterator[ChatEvent]:
        payload = self._payload(request)

        elapsed = Elapsed()
        assembly = OllamaAssembly()
        if request.stream:
            async for chunk in self._stream(payload):
                emitted = assembly.take(chunk)
                if emitted is not None:
                    yield emitted
        else:
            assembly.take(await self._complete(payload))

        reply = assembly.reply()
        logger.info(
            "ollama chat: %s replied in %dms (%d call(s))",
            self._model,
            elapsed.ms(),
            len(reply.tool_calls),
        )

        yield reply

    async def _complete(self, payload: dict[str, Any]) -> OllamaWireChunk:
        """Один запрос-ответ: всё тело /api/chat одним чанком."""
        attempts = self._cfg.http.max_retries + 1

        for attempt in range(attempts):
            try:
                response = await self._client.post(
                    self._endpoint(), json=payload, headers=self._headers()
                )
                if response.status_code in self.RETRY_STATUSES:
                    raise httpx.TransportError(f"status {response.status_code}")

                if response.is_error:
                    msg = (
                        f"chat endpoint returned {response.status_code}: "
                        f"{response.content[:500]!r}"
                    )
                    raise ChatProviderError(msg)

                break
            except ChatProviderError:
                raise
            except httpx.HTTPError as exc:
                if attempt + 1 >= attempts:
                    msg = f"chat endpoint failed: {exc}"
                    raise ChatProviderError(msg) from exc

                logger.warning(
                    "ollama chat: attempt %d/%d failed: %s",
                    attempt + 1,
                    attempts,
                    exc,
                )

        try:
            return OllamaWireChunk.model_validate_json(response.content)
        except ValidationError as exc:
            msg = f"chat endpoint returned malformed body: {response.content[:300]!r}"
            raise ChatProviderError(msg) from exc

    async def _stream(self, payload: dict[str, Any]) -> AsyncIterator[OllamaWireChunk]:
        """NDJSON-чанки ответа; до первого байта запрос повторяется."""
        attempts = self._cfg.http.max_retries + 1

        for attempt in range(attempts):
            streamed = False
            try:
                async for chunk in self._attempt(payload):
                    streamed = True
                    yield chunk

                return
            except ChatProviderError:
                raise
            except httpx.HTTPError as exc:
                if streamed:
                    msg = f"chat stream broke mid-reply: {exc}"
                    raise ChatProviderError(msg) from exc

                if attempt + 1 >= attempts:
                    msg = f"chat endpoint failed: {exc}"
                    raise ChatProviderError(msg) from exc

                logger.warning(
                    "ollama chat: attempt %d/%d failed: %s",
                    attempt + 1,
                    attempts,
                    exc,
                )

    async def _attempt(self, payload: dict[str, Any]) -> AsyncIterator[OllamaWireChunk]:
        async with self._client.stream(
            "POST", self._endpoint(), json=payload, headers=self._headers()
        ) as response:
            if response.status_code in self.RETRY_STATUSES:
                # тело не нужно: статус ретраится как сетевая ошибка
                raise httpx.TransportError(f"status {response.status_code}")

            if response.is_error:
                body = await response.aread()
                msg = f"chat endpoint returned {response.status_code}: {body[:500]!r}"
                raise ChatProviderError(msg)

            lines = response.aiter_lines()
            while True:
                line = await self._next_line(lines)
                if line is None:
                    break

                body = line.strip()
                if not body:
                    continue

                yield self._parse_line(body)

    async def _next_line(self, lines: AsyncIterator[str]) -> str | None:
        """Очередная NDJSON-строка под вотчдогом паузы между чанками."""
        ceiling = self._cfg.http.stream_chunk_timeout

        try:
            if ceiling:
                async with asyncio.timeout(ceiling):
                    return await anext(lines, None)
            return await anext(lines, None)
        except TimeoutError as exc:
            msg = f"chat stream stalled: no chunk for {ceiling}s"
            raise ChatProviderError(msg) from exc

    @staticmethod
    def _parse_line(body: str) -> OllamaWireChunk:
        try:
            return OllamaWireChunk.model_validate_json(body)
        except ValidationError as exc:
            msg = f"chat endpoint sent malformed chunk: {body[:300]!r}"
            raise ChatProviderError(msg) from exc

    def _endpoint(self) -> str:
        return self._cfg.http.base_url.rstrip("/") + "/" + self.ENDPOINT

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._cfg.http.api_key}"}

    def _payload(self, request: ChatRequest) -> dict[str, Any]:
        payload: dict[str, Any] = {
            OllamaField.MODEL.value: self._model,
            OllamaField.MESSAGES.value: self._messages(request.messages),
            OllamaField.STREAM.value: request.stream,
        }

        if request.tools:
            payload[OllamaField.TOOLS.value] = self._tools(request.tools)

        sampling = dict(request.sampling)
        options = self._options(sampling)

        payload.update(sampling)
        if options:
            payload[OllamaField.OPTIONS.value] = options

        return payload

    @classmethod
    def _options(cls, sampling: dict[str, Any]) -> dict[str, Any]:
        """Блок options тела; ключи options и stop изымаются из sampling.

        Таблица sampling уходит в тело как есть, но stop мост кладёт верхним
        уровнем по контракту конверта, а нативный формат держит его внутри
        options — стоп-последовательности моста перекрывают админские.
        """
        raw = sampling.pop(OllamaField.OPTIONS.value, None)

        options: dict[str, Any] = {}
        if raw is not None:
            if not isinstance(raw, Mapping):
                msg = f"sampling options must be a table, got: {type(raw).__name__}"
                raise ChatProviderError(msg)

            options = dict(raw)

        stop = sampling.pop(OllamaField.STOP.value, None)
        if stop is not None:
            options[OllamaField.STOP.value] = list(stop)

        return options

    @classmethod
    def _messages(cls, messages: Sequence[ChatTurn]) -> list[dict[str, Any]]:
        wired: list[dict[str, Any]] = []
        for message in messages:
            wired.append(cls._message(message))

        return wired

    @classmethod
    def _message(cls, message: ChatTurn) -> dict[str, Any]:
        wired: dict[str, Any] = {
            OllamaField.ROLE.value: message.role.value,
            OllamaField.CONTENT.value: message.content,
        }

        if message.role is ChatRole.TOOL:
            wired[OllamaField.TOOL_CALL_ID.value] = message.tool_call_id

        if message.role is ChatRole.ASSISTANT and message.reasoning is not None:
            wired[OllamaField.THINKING.value] = message.reasoning

        if message.tool_calls:
            wired[OllamaField.TOOL_CALLS.value] = cls._calls(message.tool_calls)

        return wired

    @staticmethod
    def _calls(calls: Sequence[ToolCallRequest]) -> list[dict[str, Any]]:
        wired: list[dict[str, Any]] = []
        for call in calls:
            wired.append(
                {
                    OllamaField.ID.value: call.id,
                    OllamaField.FUNCTION.value: {
                        OllamaField.NAME.value: call.name,
                        OllamaField.ARGUMENTS.value: dict(call.arguments),
                    },
                }
            )

        return wired

    @staticmethod
    def _tools(tools: Sequence[ToolSpec]) -> list[dict[str, Any]]:
        wired: list[dict[str, Any]] = []
        for tool in tools:
            wired.append(
                {
                    OllamaField.TYPE.value: OllamaField.FUNCTION.value,
                    OllamaField.FUNCTION.value: {
                        OllamaField.NAME.value: tool.name,
                        OllamaField.DESCRIPTION.value: tool.description,
                        OllamaField.PARAMETERS.value: dict(tool.parameters),
                    },
                }
            )

        return wired
