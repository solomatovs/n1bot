"""Провайдер ollama: нативный чат /api/chat потоком NDJSON.

Провайдер владеет wire-форматом: сообщения с thinking, tool_calls-объектами
и картинками, таблица sampling как есть, options с переложенным из моста
stop; форма ответа (reply_schema) уходит полем format. Сеть — LlmEndpoint
поверх HttpTransport.

Ошибки:
LlmError — endpoint недоступен, ответил статусом, мусором или чанком-ошибкой,
    оборвал поток, либо генерация завершилась не по-хорошему (done_reason
    кроме stop: length — лимит токенов, load/unload — прогон без генерации).
LlmProvidersError — секция не того провайдера или у провайдера нет
    эмбеддингов.
"""

from __future__ import annotations

import base64
import logging
from collections.abc import Mapping, Sequence
from enum import StrEnum
from typing import Any, ClassVar, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from boba.llm.chat import (
    ChatDelta,
    ChatModel,
    ChatReply,
    ChatRequest,
    ChatRole,
    ChatTurn,
    ChatUsage,
    LlmError,
    ToolCall,
)
from boba.llm.embedding import EmbeddingModel
from boba.llm.http.endpoint import (
    ChunkAssembly,
    FunctionField,
    HttpChatModel,
    HttpLlmProvider,
    LlmEndpoint,
    LlmRoute,
    WireDecoder,
)
from boba.llm.providers import (
    ChatModelConfig,
    EmbeddingModelConfig,
    LlmBackend,
    LlmProvider,
    LlmProviderManifest,
    LlmProvidersError,
)
from boba.transport.http import HttpTransport

logger = logging.getLogger(__name__)

__all__ = ["MANIFEST", "OllamaBackend", "OllamaChatModel", "OllamaProvider"]


class OllamaProvider(HttpLlmProvider):
    """Секция `[llm.<имя>]` с kind = "ollama"."""

    kind: Literal["ollama"]


class OllamaField(StrEnum):
    """Ключи wire-формата /api/chat."""

    MODEL = "model"
    MESSAGES = "messages"
    ROLE = "role"
    CONTENT = "content"
    THINKING = "thinking"
    IMAGES = "images"
    TOOLS = "tools"
    TOOL_CALLS = "tool_calls"
    TOOL_CALL_ID = "tool_call_id"
    ARGUMENTS = "arguments"
    ID = "id"
    STREAM = "stream"
    STOP = "stop"
    OPTIONS = "options"
    FORMAT = "format"


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


class OllamaDoneReason(StrEnum):
    """Известные done_reason финального чанка; полный ответ — только STOP.

    LENGTH — генерацию срезал лимит токенов; прочие значения (load, unload)
    означают прогон без генерации.
    """

    STOP = "stop"
    LENGTH = "length"


class OllamaAssembly(ChunkAssembly[OllamaWireChunk]):
    """Склейка потока чанков в финальное сообщение."""

    def __init__(self, where: str) -> None:
        self._where = where
        self._content: list[str] = []
        self._thinking: list[str] = []
        self._calls: list[ToolCall] = []
        self._input_tokens = 0
        self._output_tokens = 0
        self._done_reason = ""

    def take(self, chunk: OllamaWireChunk) -> ChatDelta | None:
        """Учитывает чанк; наружу — прирост текста или рассуждений."""
        if chunk.error:
            msg = f"{self._where}: server reported an error in the reply: {chunk.error}"
            raise LlmError(msg)

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

    def reply(self) -> ChatReply:
        self._check_complete()

        return ChatReply(
            content="".join(self._content),
            reasoning="".join(self._thinking),
            tool_calls=list(self._calls),
            usage=ChatUsage(
                input_tokens=self._input_tokens,
                output_tokens=self._output_tokens,
            ),
        )

    def _check_complete(self) -> None:
        """Обрыв генерации сервером — честная ошибка, а не тихо неполный ответ."""
        if not self._done_reason:
            return

        if self._done_reason == OllamaDoneReason.STOP:
            return

        if self._done_reason == OllamaDoneReason.LENGTH:
            msg = (
                f"{self._where}: reply hit the token ceiling: done_reason=length, "
                f"{self._output_tokens} eval tokens spent; "
                "raise the num_predict sampling option"
            )
            raise LlmError(msg)

        msg = (
            f"{self._where}: generation ended abnormally, expected done_reason=stop, "
            f"got done_reason={self._done_reason}"
        )
        raise LlmError(msg)

    @staticmethod
    def _call(call: OllamaWireCall) -> ToolCall:
        """Вызов конверта; без id от сервера вызов получает локальный uuid."""
        call_id = call.id
        if not call_id:
            call_id = uuid4().hex

        return ToolCall(
            id=call_id,
            name=call.function.name,
            arguments=dict(call.function.arguments),
        )


class NdjsonStream(WireDecoder[OllamaWireChunk]):
    """NDJSON-строки потока в чанки; экземпляр живёт один запрос."""

    def __init__(self, where: str) -> None:
        self._where = where

    def feed(self, line: str) -> OllamaWireChunk | None:
        body = line.strip()
        if not body:
            return None

        try:
            return OllamaWireChunk.model_validate_json(body)
        except ValidationError as exc:
            msg = (
                f"{self._where}: stream line is not an OllamaWireChunk: "
                f"{body[:300]!r}: {exc}"
            )
            raise LlmError(msg) from exc

    def finish(self) -> OllamaWireChunk | None:
        """NDJSON не буферизуется: недосланных чанков не бывает."""
        return None


class OllamaChatModel(HttpChatModel[OllamaWireChunk]):
    """Реализация ChatModel поверх нативного /api/chat: сообщения с thinking
    и картинками, options с переложенным stop, форма ответа полем format."""

    def _decoder(self) -> WireDecoder[OllamaWireChunk]:
        return NdjsonStream(self.where)

    def _assembly(self) -> ChunkAssembly[OllamaWireChunk]:
        return OllamaAssembly(self.where)

    def _parse_body(self, body: bytes) -> OllamaWireChunk:
        try:
            return OllamaWireChunk.model_validate_json(body)
        except ValidationError as exc:
            msg = (
                f"{self.where}: response body is not an OllamaWireChunk: "
                f"{body[:300]!r}: {exc}"
            )
            raise LlmError(msg) from exc

    def _payload(self, request: ChatRequest) -> dict[str, Any]:
        payload: dict[str, Any] = {
            OllamaField.MODEL.value: self._model,
            OllamaField.MESSAGES.value: self._messages(request.messages),
            OllamaField.STREAM.value: request.stream,
        }

        if request.tools:
            payload[OllamaField.TOOLS.value] = self._function_tools(request.tools)

        if request.reply_schema is not None:
            payload[OllamaField.FORMAT.value] = dict(request.reply_schema.parameters)

        sampling = dict(request.sampling)
        options = self._options(sampling)

        payload.update(sampling)
        if options:
            payload[OllamaField.OPTIONS.value] = options

        return payload

    def _options(self, sampling: dict[str, Any]) -> dict[str, Any]:
        """Блок options тела; ключи options и stop изымаются из sampling.

        Таблица sampling уходит в тело как есть, но stop мост кладёт верхним
        уровнем по контракту конверта, а нативный формат держит его внутри
        options — стоп-последовательности моста перекрывают админские.
        """
        raw = sampling.pop(OllamaField.OPTIONS.value, None)

        options: dict[str, Any] = {}
        if raw is not None:
            if not isinstance(raw, Mapping):
                msg = (
                    f"{self.where}: sampling.options must be a table, "
                    f"got {type(raw).__name__}: {raw!r:.200}"
                )
                raise LlmError(msg)

            options = dict(raw)

        stop = sampling.pop(OllamaField.STOP.value, None)
        if stop is not None:
            options[OllamaField.STOP.value] = list(stop)

        return options

    def _messages(self, messages: Sequence[ChatTurn]) -> list[dict[str, Any]]:
        wired: list[dict[str, Any]] = []
        for message in messages:
            wired.append(self._message(message))

        return wired

    def _message(self, message: ChatTurn) -> dict[str, Any]:
        wired: dict[str, Any] = {
            OllamaField.ROLE.value: message.role.value,
            OllamaField.CONTENT.value: message.content,
        }

        if message.images:
            images: list[str] = []
            for image in message.images:
                images.append(base64.b64encode(image.data).decode("ascii"))

            wired[OllamaField.IMAGES.value] = images

        if message.role is ChatRole.TOOL:
            wired[OllamaField.TOOL_CALL_ID.value] = message.tool_call_id

        if message.role is ChatRole.ASSISTANT and message.reasoning is not None:
            wired[OllamaField.THINKING.value] = message.reasoning

        if message.tool_calls:
            wired[OllamaField.TOOL_CALLS.value] = self._calls(message.tool_calls)

        return wired

    def _calls(self, calls: Sequence[ToolCall]) -> list[dict[str, Any]]:
        wired: list[dict[str, Any]] = []
        for call in calls:
            wired.append(
                {
                    OllamaField.ID.value: call.id,
                    FunctionField.FUNCTION.value: {
                        FunctionField.NAME.value: call.name,
                        OllamaField.ARGUMENTS.value: dict(call.arguments),
                    },
                }
            )

        return wired


class OllamaBackend(LlmBackend):
    """Реализация LlmBackend: один транспорт к серверу ollama на провайдера."""

    CHAT_LABEL: ClassVar[str] = "ollama chat"

    def __init__(self, provider: LlmProvider) -> None:
        if not isinstance(provider, OllamaProvider):
            msg = (
                f"ollama backend expects an OllamaProvider section, "
                f"got kind {provider.kind!r}"
            )
            raise LlmProvidersError(msg)

        self._provider = provider
        self._transport = HttpTransport(provider.connection, provider.transport)

    def chat(self, cfg: ChatModelConfig) -> ChatModel:
        endpoint = LlmEndpoint(
            self._transport,
            self._provider.connection,
            LlmRoute.OLLAMA_CHAT,
            self.CHAT_LABEL,
        )

        return OllamaChatModel(endpoint, cfg.model)

    def embedding(self, cfg: EmbeddingModelConfig) -> EmbeddingModel:
        msg = (
            f"ollama provider at {self._provider.connection.public_url()} "
            f"has no embedding models, asked for {cfg.model!r}"
        )
        raise LlmProvidersError(msg)

    async def aclose(self) -> None:
        await self._transport.close()


MANIFEST = LlmProviderManifest(
    kind="ollama",
    config=OllamaProvider,
    backend=OllamaBackend,
)
