"""Openai-совместимый чат-бэкенд: /chat/completions потоком SSE.

Провайдер владеет wire-форматом: сборка тела запроса, разбор SSE-дельт,
склейка вызовов инструментов по index, нормализация рассуждений
(reasoning_content | reasoning). HTTP-обмен — ретраи, вотчдог пауз —
делает ChatExchange.

Ошибки:
ChatProviderError — endpoint недоступен, ответил статусом или мусором,
    либо пауза между чанками превысила потолок конфига.
"""

from __future__ import annotations

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
from boba.llm.http import ChatExchange, WireStream
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


class FinishReason(StrEnum):
    """Причины остановки генерации, которые различает провайдер."""

    LENGTH = "length"
    """Ответ упёрся в потолок токенов и оборван на полуслове."""

    STOP = "stop"
    TOOL_CALLS = "tool_calls"


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
    finish_reason: str = ""


class WireOutputDetails(BaseModel):
    """Разбивка выходных токенов; провайдеры без рассуждений её не шлют."""

    model_config = ConfigDict(extra="ignore")

    reasoning_tokens: int = 0


class WireUsage(BaseModel):
    """usage чанка: провайдер шлёт его в финале потока."""

    model_config = ConfigDict(extra="ignore")

    prompt_tokens: int = 0
    completion_tokens: int = 0
    completion_tokens_details: WireOutputDetails = WireOutputDetails()


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
        self._finish_reason = ""

    def take(self, chunk: WireChunk) -> ChatDelta | None:
        """Учитывает чанк; наружу — прирост текста или рассуждений."""
        if chunk.usage is not None:
            self._usage = chunk.usage

        if not chunk.choices:
            return None

        choice = chunk.choices[0]
        if choice.finish_reason:
            self._finish_reason = choice.finish_reason

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

    def truncated(self) -> bool:
        """Ответ оборван потолком токенов, а не завершён моделью."""
        return self._finish_reason == FinishReason.LENGTH

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
                reasoning_tokens=self._usage.completion_tokens_details.reasoning_tokens,
            ),
        )

    def _arguments(self, growing: GrowingCall) -> dict[str, Any]:
        if not growing.arguments:
            return {}

        try:
            parsed = json.loads(growing.arguments)
        except json.JSONDecodeError as exc:
            raise self._broken_call(growing) from exc

        if not isinstance(parsed, dict):
            msg = f"provider sent non-object call arguments: {growing.name}"
            raise ChatProviderError(msg)

        return parsed

    def _broken_call(self, growing: GrowingCall) -> ChatProviderError:
        """Ошибка по причине обрыва: потолок токенов или мусор от провайдера."""
        if self._finish_reason != FinishReason.LENGTH:
            msg = f"provider sent malformed call arguments: {growing.name}"
            return ChatProviderError(msg)

        msg = (
            f"reply hit the token ceiling before the call was complete: "
            f"{growing.name} cut off after {len(growing.arguments)} chars "
            f"({self._usage.completion_tokens} completion tokens spent); "
            f"raise sampling max_tokens or lower the reasoning effort"
        )
        return ChatProviderError(msg)


class SseWireStream(WireStream[WireChunk]):
    """SSE-строки потока в WireChunk; экземпляр живёт одну попытку запроса."""

    SSE_DONE: ClassVar[str] = "[DONE]"
    """Данные последнего события потока."""

    def __init__(self) -> None:
        self._decoder = SSEDecoder()

    def feed(self, line: str) -> WireChunk | None:
        event = self._decoder.decode(line)
        if event is None:
            return None

        return self._parse_event(event)

    def finish(self) -> WireChunk | None:
        """Поток оборвался без пустой строки: недосланное событие всё же отдаём."""
        trailing = self._decoder.decode("")
        if trailing is None:
            return None

        return self._parse_event(trailing)

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


class OpenAiChatProvider(ChatProvider):
    """ChatProvider поверх openai-совместимого endpoint'а."""

    ENDPOINT: ClassVar[str] = "chat/completions"

    def __init__(
        self,
        cfg: OpenAiChatConfig,
        client: httpx.AsyncClient,
        model: str,
    ) -> None:
        self._cfg = cfg
        self._model = model

        endpoint = cfg.base_url.rstrip("/") + "/" + self.ENDPOINT
        self._exchange = ChatExchange(
            cfg.http,
            client,
            endpoint=endpoint,
            api_key=cfg.api_key,
            label="openai chat",
        )

    async def chat(self, request: ChatRequest) -> AsyncIterator[ChatEvent]:
        payload = self._payload(request)

        elapsed = Elapsed()
        assembly = StreamAssembly()
        if request.stream:
            async for chunk in self._exchange.stream(payload, SseWireStream):
                emitted = assembly.take(chunk)
                if emitted is not None:
                    yield emitted
        else:
            body = await self._exchange.complete(payload)
            assembly.take(self._parse_body(body))

        reply = assembly.reply()
        if assembly.truncated():
            logger.warning(
                "openai chat: %s hit the token ceiling, reply is cut off "
                "(%d completion tokens)",
                self._model,
                reply.usage.output_tokens,
            )

        logger.info(
            "openai chat: %s replied in %dms (%d call(s))",
            self._model,
            elapsed.ms(),
            len(reply.tool_calls),
        )

        yield reply

    @staticmethod
    def _parse_body(body: bytes) -> WireChunk:
        try:
            return WireChunk.model_validate_json(body)
        except ValidationError as exc:
            msg = f"chat endpoint returned malformed body: {body[:300]!r}"
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
