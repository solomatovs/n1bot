"""Провайдер openai-совместимого API: чат /chat/completions потоком SSE и
эмбеддинги /embeddings.

Провайдер владеет wire-форматом: тело запроса, разбор SSE-дельт, склейка
вызовов инструментов по index, нормализация рассуждений (reasoning_content |
reasoning). Форма ответа (reply_schema) уходит объявлением функции с
tool_choice на неё: response_format роутеры отклоняют. Сеть — LlmEndpoint
поверх HttpTransport.

Ошибки:
LlmError — endpoint недоступен, ответил статусом или мусором, оборвал поток,
    либо генерация завершилась не по-хорошему (finish_reason вне списка
    полных: лимит токенов, контент-фильтр, авария провайдера).
LlmProvidersError — секция не того провайдера.
"""

from __future__ import annotations

import base64
import json
import logging
from collections.abc import Sequence
from enum import StrEnum
from typing import Any, ClassVar, Literal

from httpx_sse import ServerSentEvent

# декодер построчный, а не EventSource: вотчдог паузы считает каждую строку,
# включая keepalive-комментарии прокси, и content-type сервера не проверяется
from httpx_sse._decoders import SSEDecoder
from pydantic import BaseModel, ConfigDict, ValidationError, field_validator

from boba.llm.chat import (
    ChatDelta,
    ChatImage,
    ChatModel,
    ChatReply,
    ChatRequest,
    ChatRole,
    ChatTurn,
    ChatUsage,
    LlmError,
    ToolCall,
    ToolSpec,
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
from boba.toolkit.timing import Elapsed
from boba.transport.http import HttpTransport

logger = logging.getLogger(__name__)

__all__ = [
    "MANIFEST",
    "OpenAiBackend",
    "OpenAiChatModel",
    "OpenAiEmbeddingModel",
    "OpenAiProvider",
]


class OpenAiProvider(HttpLlmProvider):
    """Секция `[llm.<имя>]` с kind = "openai"."""

    kind: Literal["openai"]


class WireField(StrEnum):
    """Ключи wire-формата chat/completions и embeddings."""

    MODEL = "model"
    MESSAGES = "messages"
    ROLE = "role"
    CONTENT = "content"
    REASONING_CONTENT = "reasoning_content"
    REASONING = "reasoning"
    TOOLS = "tools"
    TOOL_CHOICE = "tool_choice"
    TOOL_CALLS = "tool_calls"
    TOOL_CALL_ID = "tool_call_id"
    TYPE = "type"
    ARGUMENTS = "arguments"
    ID = "id"
    STREAM = "stream"
    TEXT = "text"
    IMAGE_URL = "image_url"
    URL = "url"
    INPUT = "input"


class ContentPartType(StrEnum):
    """Виды частей содержимого сообщения с картинками."""

    TEXT = "text"
    IMAGE_URL = "image_url"


class FinishReason(StrEnum):
    """Известные finish_reason ответа; всё вне списка полных — обрыв.

    Полные: STOP (обычный конец или стоп-последовательность), TOOL_CALLS и
    устаревший FUNCTION_CALL. LENGTH — генерацию срезал лимит токенов,
    CONTENT_FILTER — ответ снял фильтр провайдера; прочие значения
    (insufficient_system_resource у deepseek, error у openrouter, ...)
    провайдероспецифичны и означают аварию генерации.
    """

    STOP = "stop"
    TOOL_CALLS = "tool_calls"
    FUNCTION_CALL = "function_call"
    LENGTH = "length"
    CONTENT_FILTER = "content_filter"

    @classmethod
    def is_complete(cls, reason: str) -> bool:
        return reason in (cls.STOP, cls.TOOL_CALLS, cls.FUNCTION_CALL)


class WireFunctionDelta(BaseModel):
    """function внутри дельты вызова."""

    model_config = ConfigDict(extra="ignore")

    name: str = ""
    arguments: str = ""

    @field_validator("name", "arguments", mode="before")
    @classmethod
    def _null_as_empty(cls, value: object) -> object:
        if value is None:
            return ""

        return value


class WireCallDelta(BaseModel):
    """Дельта вызова инструмента: копится по index."""

    model_config = ConfigDict(extra="ignore")

    index: int = 0
    id: str = ""
    function: WireFunctionDelta = WireFunctionDelta()


class WireDelta(BaseModel):
    """delta одного SSE-чанка; провайдеры шлют null вместо отсутствующего поля."""

    model_config = ConfigDict(extra="ignore")

    content: str = ""
    reasoning_content: str = ""
    reasoning: str = ""
    tool_calls: Sequence[WireCallDelta] = ()

    @field_validator("content", "reasoning_content", "reasoning", mode="before")
    @classmethod
    def _null_text_as_empty(cls, value: object) -> object:
        if value is None:
            return ""

        return value

    @field_validator("tool_calls", mode="before")
    @classmethod
    def _null_calls_as_empty(cls, value: object) -> object:
        if value is None:
            return ()

        return value

    def reasoning_text(self) -> str:
        if self.reasoning_content:
            return self.reasoning_content

        return self.reasoning


class WireChoice(BaseModel):
    """Вариант чанка; message приходит в нестримящем ответе."""

    model_config = ConfigDict(extra="ignore")

    delta: WireDelta = WireDelta()
    message: WireDelta | None = None
    # null в каждом промежуточном чанке — норма провода, поэтому не str
    finish_reason: str | None = None


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


class StreamAssembly(ChunkAssembly[WireChunk]):
    """Склейка потока дельт в финальное сообщение."""

    def __init__(self, where: str) -> None:
        self._where = where
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

    def reply(self) -> ChatReply:
        self._check_complete()

        calls: list[ToolCall] = []
        for index in sorted(self._calls):
            growing = self._calls[index]
            calls.append(
                ToolCall(
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

    def _check_complete(self) -> None:
        """Обрыв генерации провайдером — честная ошибка, а не тихо неполный
        ответ или битый JSON недописанного вызова инструмента."""
        if not self._finish_reason:
            return

        if FinishReason.is_complete(self._finish_reason):
            return

        if self._finish_reason == FinishReason.LENGTH:
            raise self._ceiling_error()

        if self._finish_reason == FinishReason.CONTENT_FILTER:
            msg = (
                f"{self._where}: reply blocked by the provider content filter "
                "(finish_reason=content_filter)"
            )
            raise LlmError(msg)

        msg = (
            f"{self._where}: generation ended abnormally, expected "
            f"finish_reason=stop or tool_calls, got finish_reason={self._finish_reason}"
        )
        raise LlmError(msg)

    def _ceiling_error(self) -> LlmError:
        """Ошибка обрыва по потолку токенов: расход, недописанный вызов, совет."""
        spent = self._usage.completion_tokens
        reasoning = self._usage.completion_tokens_details.reasoning_tokens
        msg = (
            f"{self._where}: reply hit the token ceiling: finish_reason=length, "
            f"{spent} completion tokens spent ({reasoning} reasoning)"
        )

        if cut := self._cut_call():
            msg = f"{msg}; call {cut} is cut off mid-arguments"

        msg = f"{msg}; raise sampling max_tokens or lower the reasoning effort"

        return LlmError(msg)

    def _cut_call(self) -> str:
        """Имя вызова, который резался последним; пусто, если все вызовы целы."""
        if not self._calls:
            return ""

        last = self._calls[max(self._calls)]

        try:
            json.loads(last.arguments)
        except json.JSONDecodeError:
            return last.name

        return ""

    def _arguments(self, growing: GrowingCall) -> dict[str, Any]:
        if not growing.arguments:
            return {}

        try:
            parsed = json.loads(growing.arguments)
        except json.JSONDecodeError as exc:
            msg = (
                f"{self._where}: provider sent malformed call arguments for "
                f"{growing.name}: {exc}: {growing.arguments[:200]!r}"
            )
            raise LlmError(msg) from exc

        if not isinstance(parsed, dict):
            msg = (
                f"{self._where}: provider sent non-object call arguments for "
                f"{growing.name}, expected a json object, got "
                f"{type(parsed).__name__}: {growing.arguments[:200]!r}"
            )
            raise LlmError(msg)

        return parsed


class SseStream(WireDecoder[WireChunk]):
    """SSE-строки потока в WireChunk; экземпляр живёт один запрос."""

    def __init__(self, where: str) -> None:
        self._where = where
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

    def _parse_event(self, event: ServerSentEvent) -> WireChunk | None:
        """Чанк из события; [DONE] и пустые данные чанком не являются."""
        body = event.data.strip()
        if not body:
            return None

        if body == "[DONE]":
            return None

        try:
            return WireChunk.model_validate_json(body)
        except ValidationError as exc:
            msg = f"{self._where}: sse data is not a WireChunk: {body[:300]!r}: {exc}"
            raise LlmError(msg) from exc


class OpenAiChatModel(HttpChatModel[WireChunk]):
    """Реализация ChatModel поверх openai-совместимого /chat/completions:
    сообщения, инструменты и форма ответа в wire-формате, SSE-дельты в сборку."""

    IMAGE_URL_TEMPLATE: ClassVar[str] = "data:{media_type};base64,{payload}"

    def _decoder(self) -> WireDecoder[WireChunk]:
        return SseStream(self.where)

    def _assembly(self) -> ChunkAssembly[WireChunk]:
        return StreamAssembly(self.where)

    def _parse_body(self, body: bytes) -> WireChunk:
        try:
            return WireChunk.model_validate_json(body)
        except ValidationError as exc:
            msg = (
                f"{self.where}: response body is not a WireChunk: {body[:300]!r}: {exc}"
            )
            raise LlmError(msg) from exc

    def _payload(self, request: ChatRequest) -> dict[str, Any]:
        payload: dict[str, Any] = {
            WireField.MODEL.value: self._model,
            WireField.MESSAGES.value: self._messages(request.messages),
            WireField.STREAM.value: request.stream,
        }

        tools: list[ToolSpec] = list(request.tools)
        if request.reply_schema is not None:
            tools.append(request.reply_schema)
            payload[WireField.TOOL_CHOICE.value] = self._choice(request.reply_schema)

        if tools:
            payload[WireField.TOOLS.value] = self._function_tools(tools)

        # админская таблица сэмплинга как есть: без проверок и переименований
        payload.update(dict(request.sampling))

        return payload

    def _messages(self, messages: Sequence[ChatTurn]) -> list[dict[str, Any]]:
        wired: list[dict[str, Any]] = []
        for message in messages:
            wired.append(self._message(message))

        return wired

    def _message(self, message: ChatTurn) -> dict[str, Any]:
        wired: dict[str, Any] = {
            WireField.ROLE.value: message.role.value,
            WireField.CONTENT.value: self._content(message),
        }

        if message.role is ChatRole.TOOL:
            wired[WireField.TOOL_CALL_ID.value] = message.tool_call_id

        if message.role is ChatRole.ASSISTANT and message.reasoning is not None:
            # провайдер в режиме размышления требует вернуть рассуждения
            # у каждого сообщения ассистента, в том числе пустые
            wired[WireField.REASONING_CONTENT.value] = message.reasoning

        if message.tool_calls:
            wired[WireField.TOOL_CALLS.value] = self._calls(message.tool_calls)

        return wired

    def _content(self, message: ChatTurn) -> str | list[dict[str, Any]]:
        """Текст как есть; с картинками — список частей."""
        if not message.images:
            return message.content

        parts: list[dict[str, Any]] = [
            {
                WireField.TYPE.value: ContentPartType.TEXT.value,
                WireField.TEXT.value: message.content,
            }
        ]
        for image in message.images:
            parts.append(
                {
                    WireField.TYPE.value: ContentPartType.IMAGE_URL.value,
                    WireField.IMAGE_URL.value: {
                        WireField.URL.value: self._data_url(image)
                    },
                }
            )

        return parts

    def _data_url(self, image: ChatImage) -> str:
        encoded = base64.b64encode(image.data).decode("ascii")

        return self.IMAGE_URL_TEMPLATE.format(
            media_type=image.media_type, payload=encoded
        )

    def _calls(self, calls: Sequence[ToolCall]) -> list[dict[str, Any]]:
        wired: list[dict[str, Any]] = []
        for call in calls:
            wired.append(
                {
                    WireField.ID.value: call.id,
                    FunctionField.TYPE.value: FunctionField.FUNCTION.value,
                    FunctionField.FUNCTION.value: {
                        FunctionField.NAME.value: call.name,
                        WireField.ARGUMENTS.value: json.dumps(
                            dict(call.arguments), ensure_ascii=False
                        ),
                    },
                }
            )

        return wired

    def _choice(self, schema: ToolSpec) -> dict[str, Any]:
        return {
            FunctionField.TYPE.value: FunctionField.FUNCTION.value,
            FunctionField.FUNCTION.value: {FunctionField.NAME.value: schema.name},
        }


class WireVector(BaseModel):
    """Один вектор из data[] ответа /embeddings."""

    index: int
    embedding: list[float]


class WireEmbeddings(BaseModel):
    """Тело ответа /embeddings; лишние поля провайдера игнорируются."""

    model_config = ConfigDict(extra="ignore")

    data: list[WireVector]


class OpenAiEmbeddingModel(EmbeddingModel):
    """Реализация EmbeddingModel поверх openai-совместимого /embeddings.

    Endpoint симметричный: запрос и документы эмбеддятся одинаково, без
    префиксов.
    """

    def __init__(self, endpoint: LlmEndpoint, cfg: EmbeddingModelConfig) -> None:
        self._endpoint = endpoint
        self._cfg = cfg

    async def embed_documents(
        self,
        contents: Sequence[str],
    ) -> Sequence[Sequence[float]]:
        vectors: list[Sequence[float]] = []
        elapsed = Elapsed()
        logged = 0

        for start in range(0, len(contents), self._cfg.batch_size):
            batch = list(contents[start : start + self._cfg.batch_size])
            vectors.extend(await self._request(batch))

            if len(vectors) - logged >= self._cfg.progress_every:
                logged = len(vectors)
                logger.info(
                    "embedding progress: %d/%d in %dms",
                    len(vectors),
                    len(contents),
                    elapsed.ms(),
                )

        return vectors

    async def embed_query(self, content: str) -> Sequence[float]:
        vectors = await self._request([content])

        return vectors[0]

    def dim(self) -> int:
        return self._cfg.dim

    async def _request(self, batch: Sequence[str]) -> list[Sequence[float]]:
        payload = {
            WireField.MODEL.value: self._cfg.model,
            WireField.INPUT.value: list(batch),
        }

        elapsed = Elapsed()
        body = await self._endpoint.post(payload)
        logger.info("embeddings request: %d inputs in %dms", len(batch), elapsed.ms())

        return self._vectors(self._parse(body), expected=len(batch))

    def _parse(self, body: bytes) -> WireEmbeddings:
        try:
            return WireEmbeddings.model_validate_json(body)
        except ValidationError as exc:
            msg = (
                f"{self._endpoint.where} returned a body that is not an embeddings "
                f"reply: {body[:200]!r}: {exc}"
            )
            raise LlmError(msg) from exc

    def _vectors(self, reply: WireEmbeddings, expected: int) -> list[Sequence[float]]:
        if len(reply.data) != expected:
            msg = (
                f"{self._endpoint.where} returned {len(reply.data)} vectors "
                f"for {expected} inputs"
            )
            raise LlmError(msg)

        ordered = sorted(reply.data, key=self._index_of)

        vectors: list[Sequence[float]] = []
        for item in ordered:
            self._check_dim(item.embedding)
            vectors.append(item.embedding)

        return vectors

    @staticmethod
    def _index_of(item: WireVector) -> int:
        return item.index

    def _check_dim(self, vec: Sequence[float]) -> None:
        actual = len(vec)
        if actual != self._cfg.dim:
            msg = (
                f"{self._endpoint.where}: model {self._cfg.model!r} returned dim "
                f"{actual}, config declares {self._cfg.dim}"
            )
            raise LlmError(msg)


class OpenAiBackend(LlmBackend):
    """Реализация LlmBackend: один транспорт к API на провайдера."""

    CHAT_LABEL: ClassVar[str] = "openai chat"
    EMBEDDING_LABEL: ClassVar[str] = "openai embeddings"

    def __init__(self, provider: LlmProvider) -> None:
        if not isinstance(provider, OpenAiProvider):
            msg = (
                f"openai backend expects an OpenAiProvider section, "
                f"got kind {provider.kind!r}"
            )
            raise LlmProvidersError(msg)

        self._provider = provider
        self._transport = HttpTransport(provider.connection, provider.transport)

    def chat(self, cfg: ChatModelConfig) -> ChatModel:
        endpoint = self._endpoint(LlmRoute.CHAT_COMPLETIONS, self.CHAT_LABEL)

        return OpenAiChatModel(endpoint, cfg.model)

    def embedding(self, cfg: EmbeddingModelConfig) -> EmbeddingModel:
        endpoint = self._endpoint(LlmRoute.EMBEDDINGS, self.EMBEDDING_LABEL)

        return OpenAiEmbeddingModel(endpoint, cfg)

    async def aclose(self) -> None:
        await self._transport.close()

    def _endpoint(self, route: LlmRoute, label: str) -> LlmEndpoint:
        return LlmEndpoint(self._transport, self._provider.connection, route, label)


MANIFEST = LlmProviderManifest(
    kind="openai",
    config=OpenAiProvider,
    backend=OpenAiBackend,
)
