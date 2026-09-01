"""Openai-совместимый чат-бэкенд: /chat/completions потоком SSE.

Провайдер владеет wire-форматом целиком: сборка тела запроса, разбор
SSE-дельт, склейка вызовов инструментов по index, нормализация рассуждений
(reasoning_content | reasoning), вотчдог пауз между чанками и повторы
до первого полученного байта.

Ошибки:
ChatProviderError — endpoint недоступен, ответил статусом или мусором,
    либо пауза между чанками превысила потолок конфига.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator, Mapping, Sequence
from enum import StrEnum
from typing import Any, ClassVar

import httpx
from httpx_sse import ServerSentEvent

# декодер построчный, а не EventSource: вотчдог паузы считает каждую строку,
# включая keepalive-комментарии прокси, и content-type сервера не проверяется
from httpx_sse._decoders import SSEDecoder
from pydantic import BaseModel, ConfigDict, ValidationError

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
    OpenAiChatConfig,
    ToolCallRequest,
    ToolSpec,
)
from boba.toolkit.timing import Elapsed

logger = logging.getLogger(__name__)

__all__ = ["OpenAiChatProvider"]


class WireField(StrEnum):
    """Ключи wire-формата chat/completions."""

    MODEL = "model"
    MESSAGES = "messages"
    ROLE = "role"
    CONTENT = "content"
    REASONING_CONTENT = "reasoning_content"
    REASONING = "reasoning"
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


class WireFunctionDelta(BaseModel):
    """function внутри дельты вызова."""

    model_config = ConfigDict(extra="ignore")

    name: str = ""
    arguments: str = ""


class WireCallDelta(BaseModel):
    """Дельта вызова инструмента: копится по index."""

    model_config = ConfigDict(extra="ignore")

    index: int = 0
    id: str = ""
    function: WireFunctionDelta = WireFunctionDelta()


class WireDelta(BaseModel):
    """delta одного SSE-чанка."""

    model_config = ConfigDict(extra="ignore")

    content: str = ""
    reasoning_content: str = ""
    reasoning: str = ""
    tool_calls: Sequence[WireCallDelta] = ()

    def reasoning_text(self) -> str:
        if self.reasoning_content:
            return self.reasoning_content

        return self.reasoning


class WireChoice(BaseModel):
    """Вариант чанка; message приходит в нестримящем ответе."""

    model_config = ConfigDict(extra="ignore")

    delta: WireDelta = WireDelta()
    message: WireDelta | None = None


class WireUsage(BaseModel):
    """usage чанка: провайдер шлёт его в финале потока."""

    model_config = ConfigDict(extra="ignore")

    prompt_tokens: int = 0
    completion_tokens: int = 0


class WireChunk(BaseModel):
    """Один SSE-чанк или всё тело нестримящего ответа."""

    model_config = ConfigDict(extra="ignore")

    choices: Sequence[WireChoice] = ()
    usage: WireUsage | None = None


class GrowingCall(BaseModel):
    """Вызов инструмента, растущий из дельт."""

    id: str = ""
    name: str = ""
    arguments: str = ""


class StreamAssembly:
    """Склейка потока дельт в финальное сообщение."""

    def __init__(self) -> None:
        self._content: list[str] = []
        self._reasoning: list[str] = []
        self._calls: dict[int, GrowingCall] = {}
        self._usage = WireUsage()

    def take(self, chunk: WireChunk) -> ChatDelta | None:
        """Учитывает чанк; наружу — прирост текста или рассуждений."""
        if chunk.usage is not None:
            self._usage = chunk.usage

        if not chunk.choices:
            return None

        choice = chunk.choices[0]
        delta = choice.delta
        if choice.message is not None:
            delta = choice.message

        reasoning = delta.reasoning_text()
        if reasoning:
            self._reasoning.append(reasoning)

        if delta.content:
            self._content.append(delta.content)

        self._grow_calls(delta.tool_calls)

        if not delta.content and not reasoning:
            return None

        return ChatDelta(content=delta.content, reasoning=reasoning)

    def _grow_calls(self, calls: Sequence[WireCallDelta]) -> None:
        for call in calls:
            growing = self._calls.setdefault(call.index, GrowingCall())
            if call.id:
                growing.id = call.id
            if call.function.name:
                growing.name += call.function.name
            if call.function.arguments:
                growing.arguments += call.function.arguments

    def reply(self) -> ChatReply:
        calls: list[ToolCallRequest] = []
        for index in sorted(self._calls):
            growing = self._calls[index]
            calls.append(
                ToolCallRequest(
                    id=growing.id,
                    name=growing.name,
                    arguments=self._arguments(growing),
                )
            )

        return ChatReply(
            content="".join(self._content),
            reasoning="".join(self._reasoning),
            tool_calls=calls,
            usage=ChatUsage(
                input_tokens=self._usage.prompt_tokens,
                output_tokens=self._usage.completion_tokens,
            ),
        )

    @staticmethod
    def _arguments(growing: GrowingCall) -> dict[str, Any]:
        if not growing.arguments:
            return {}

        try:
            parsed = json.loads(growing.arguments)
        except json.JSONDecodeError as exc:
            msg = f"provider sent malformed call arguments: {growing.name}"
            raise ChatProviderError(msg) from exc

        if not isinstance(parsed, dict):
            msg = f"provider sent non-object call arguments: {growing.name}"
            raise ChatProviderError(msg)

        return parsed


class OpenAiChatProvider(ChatProvider):
    """ChatProvider поверх openai-совместимого endpoint'а."""

    ENDPOINT: ClassVar[str] = "chat/completions"

    RETRY_STATUSES: ClassVar[frozenset[int]] = frozenset({429, 500, 502, 503, 504})

    SSE_DONE: ClassVar[str] = "[DONE]"
    """Данные последнего события потока."""

    def __init__(
        self,
        cfg: OpenAiChatConfig,
        client: httpx.AsyncClient,
        model: str,
    ) -> None:
        self._cfg = cfg
        self._client = client
        self._model = model

    async def chat(self, request: ChatRequest) -> AsyncIterator[ChatEvent]:
        payload = self._payload(request)

        elapsed = Elapsed()
        assembly = StreamAssembly()
        if request.stream:
            async for chunk in self._stream(payload):
                emitted = assembly.take(chunk)
                if emitted is not None:
                    yield emitted
        else:
            assembly.take(await self._complete(payload))

        reply = assembly.reply()
        logger.info(
            "openai chat: %s replied in %dms (%d call(s))",
            self._model,
            elapsed.ms(),
            len(reply.tool_calls),
        )

        yield reply

    async def _complete(self, payload: dict[str, Any]) -> WireChunk:
        """Один запрос-ответ: всё тело chat/completions одним чанком."""
        headers = {"Authorization": f"Bearer {self._cfg.openai.api_key}"}
        endpoint = self._cfg.openai.base_url.rstrip("/") + "/" + self.ENDPOINT
        attempts = self._cfg.openai.max_retries + 1

        for attempt in range(attempts):
            try:
                response = await self._client.post(
                    endpoint, json=payload, headers=headers
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
                    "openai chat: attempt %d/%d failed: %s",
                    attempt + 1,
                    attempts,
                    exc,
                )

        try:
            return WireChunk.model_validate_json(response.content)
        except ValidationError as exc:
            msg = f"chat endpoint returned malformed body: {response.content[:300]!r}"
            raise ChatProviderError(msg) from exc

    async def _stream(self, payload: dict[str, Any]) -> AsyncIterator[WireChunk]:
        """SSE-чанки ответа; до первого байта запрос повторяется."""
        attempts = self._cfg.openai.max_retries + 1

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
                    "openai chat: attempt %d/%d failed: %s",
                    attempt + 1,
                    attempts,
                    exc,
                )

    async def _attempt(self, payload: dict[str, Any]) -> AsyncIterator[WireChunk]:
        headers = {"Authorization": f"Bearer {self._cfg.openai.api_key}"}
        endpoint = self._cfg.openai.base_url.rstrip("/") + "/" + self.ENDPOINT

        async with self._client.stream(
            "POST", endpoint, json=payload, headers=headers
        ) as response:
            if response.status_code in self.RETRY_STATUSES:
                # тело не нужно: статус ретраится как сетевая ошибка
                raise httpx.TransportError(f"status {response.status_code}")

            if response.is_error:
                body = await response.aread()
                msg = f"chat endpoint returned {response.status_code}: {body[:500]!r}"
                raise ChatProviderError(msg)

            decoder = SSEDecoder()
            lines = response.aiter_lines()
            while True:
                line = await self._next_line(lines)
                if line is None:
                    break

                event = decoder.decode(line)
                if event is None:
                    continue

                chunk = self._parse_event(event)
                if chunk is None:
                    continue

                yield chunk

            # поток оборвался без пустой строки: недосланное событие всё же отдаём
            trailing = decoder.decode("")
            if trailing is None:
                return

            chunk = self._parse_event(trailing)
            if chunk is None:
                return

            yield chunk

    async def _next_line(self, lines: AsyncIterator[str]) -> str | None:
        """Очередная строка SSE под вотчдогом паузы между чанками."""
        ceiling = self._cfg.openai.stream_chunk_timeout

        try:
            if ceiling:
                async with asyncio.timeout(ceiling):
                    return await anext(lines, None)
            return await anext(lines, None)
        except TimeoutError as exc:
            msg = f"chat stream stalled: no chunk for {ceiling}s"
            raise ChatProviderError(msg) from exc

    @classmethod
    def _parse_event(cls, event: ServerSentEvent) -> WireChunk | None:
        """Чанк из события; [DONE] и пустые данные чанком не являются."""
        body = event.data.strip()
        if not body:
            return None

        if body == cls.SSE_DONE:
            return None

        try:
            return WireChunk.model_validate_json(body)
        except ValidationError as exc:
            msg = f"chat endpoint sent malformed chunk: {body[:300]!r}"
            raise ChatProviderError(msg) from exc

    def _payload(self, request: ChatRequest) -> dict[str, Any]:
        payload: dict[str, Any] = {
            WireField.MODEL.value: self._model,
            WireField.MESSAGES.value: self._messages(request.messages),
            WireField.STREAM.value: request.stream,
        }

        if request.tools:
            payload[WireField.TOOLS.value] = self._tools(request.tools)

        payload.update(self._sampling(request.sampling))

        return payload

    @classmethod
    def _messages(cls, messages: Sequence[ChatTurn]) -> list[dict[str, Any]]:
        wired: list[dict[str, Any]] = []
        for message in messages:
            wired.append(cls._message(message))

        return wired

    @classmethod
    def _message(cls, message: ChatTurn) -> dict[str, Any]:
        wired: dict[str, Any] = {
            WireField.ROLE.value: message.role.value,
            WireField.CONTENT.value: message.content,
        }

        if message.role is ChatRole.TOOL:
            wired[WireField.TOOL_CALL_ID.value] = message.tool_call_id

        if message.role is ChatRole.ASSISTANT and message.reasoning is not None:
            # провайдер в режиме размышления требует вернуть рассуждения
            # у каждого сообщения ассистента, в том числе пустые
            wired[WireField.REASONING_CONTENT.value] = message.reasoning

        if message.tool_calls:
            wired[WireField.TOOL_CALLS.value] = cls._calls(message.tool_calls)

        return wired

    @staticmethod
    def _calls(calls: Sequence[ToolCallRequest]) -> list[dict[str, Any]]:
        wired: list[dict[str, Any]] = []
        for call in calls:
            wired.append(
                {
                    WireField.ID.value: call.id,
                    WireField.TYPE.value: WireField.FUNCTION.value,
                    WireField.FUNCTION.value: {
                        WireField.NAME.value: call.name,
                        WireField.ARGUMENTS.value: json.dumps(
                            dict(call.arguments), ensure_ascii=False
                        ),
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
                    WireField.TYPE.value: WireField.FUNCTION.value,
                    WireField.FUNCTION.value: {
                        WireField.NAME.value: tool.name,
                        WireField.DESCRIPTION.value: tool.description,
                        WireField.PARAMETERS.value: dict(tool.parameters),
                    },
                }
            )

        return wired

    @staticmethod
    def _sampling(sampling: Mapping[str, Any]) -> dict[str, Any]:
        """Админская таблица сэмплинга как есть: без проверок и переименований."""
        return dict(sampling)
