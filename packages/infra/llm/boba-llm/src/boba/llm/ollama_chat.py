"""Нативный чат-бэкенд ollama: /api/chat потоком NDJSON.

Провайдер владеет wire-форматом: сборка тела запроса (сообщения с thinking
и tool_calls-объектами, таблица sampling как есть, options с переложенным
из моста stop), построчный разбор NDJSON-чанков. HTTP-обмен — ретраи,
вотчдог пауз — делает ChatExchange.

Ошибки:
ChatProviderError — endpoint недоступен, ответил статусом, мусором или
    чанком-ошибкой, либо пауза между чанками превысила потолок конфига.
"""

from __future__ import annotations

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
from boba.llm.http import ChatExchange, WireStream
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


class OllamaDone(StrEnum):
    """Причины остановки генерации, которые различает провайдер."""

    LENGTH = "length"
    """Ответ упёрся в num_predict и оборван на полуслове."""

    STOP = "stop"


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
    done_reason: str = ""
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
        self._done_reason = ""

    def take(self, chunk: OllamaWireChunk) -> ChatDelta | None:
        """Учитывает чанк; наружу — прирост текста или рассуждений."""
        if chunk.error:
            msg = f"chat endpoint reported: {chunk.error}"
            raise ChatProviderError(msg)

        if chunk.done:
            self._input_tokens = chunk.prompt_eval_count
            self._output_tokens = chunk.eval_count
            self._done_reason = chunk.done_reason

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

    def truncated(self) -> bool:
        """Ответ оборван потолком num_predict, а не завершён моделью."""
        return self._done_reason == OllamaDone.LENGTH

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


class NdjsonWireStream(WireStream[OllamaWireChunk]):
    """NDJSON-строки потока в чанки; экземпляр живёт одну попытку запроса."""

    def feed(self, line: str) -> OllamaWireChunk | None:
        body = line.strip()
        if not body:
            return None

        try:
            return OllamaWireChunk.model_validate_json(body)
        except ValidationError as exc:
            msg = f"chat endpoint sent malformed chunk: {body[:300]!r}"
            raise ChatProviderError(msg) from exc

    def finish(self) -> OllamaWireChunk | None:
        """NDJSON не буферизуется: недосланных чанков не бывает."""
        return None


class OllamaChatProvider(ChatProvider):
    """ChatProvider поверх нативного endpoint'а ollama."""

    ENDPOINT: ClassVar[str] = "api/chat"

    def __init__(
        self,
        cfg: OllamaChatConfig,
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
            label="ollama chat",
        )

    async def chat(self, request: ChatRequest) -> AsyncIterator[ChatEvent]:
        payload = self._payload(request)

        elapsed = Elapsed()
        assembly = OllamaAssembly()
        if request.stream:
            async for chunk in self._exchange.stream(payload, NdjsonWireStream):
                emitted = assembly.take(chunk)
                if emitted is not None:
                    yield emitted
        else:
            body = await self._exchange.complete(payload)
            assembly.take(self._parse_body(body))

        reply = assembly.reply()
        if assembly.truncated():
            logger.warning(
                "ollama chat: %s hit num_predict, reply is cut off "
                "(%d eval tokens)",
                self._model,
                reply.usage.output_tokens,
            )

        logger.info(
            "ollama chat: %s replied in %dms (%d call(s))",
            self._model,
            elapsed.ms(),
            len(reply.tool_calls),
        )

        yield reply

    @staticmethod
    def _parse_body(body: bytes) -> OllamaWireChunk:
        try:
            return OllamaWireChunk.model_validate_json(body)
        except ValidationError as exc:
            msg = f"chat endpoint returned malformed body: {body[:300]!r}"
            raise ChatProviderError(msg) from exc

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
