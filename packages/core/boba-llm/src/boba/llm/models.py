"""Модели LLM-протокола: типизированные сообщения, tool-calls, request/context."""

from __future__ import annotations

import json
from abc import ABC
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Annotated, Any, Literal, NewType, TypeAlias
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from boba.llm.errors import LLMProtocolError
from boba.tools.domain import ToolResult, ToolSchema

__all__ = [
    "AssistantBlock",
    "AssistantMessage",
    "AssistantMessageChunk",
    "DialogMessage",
    "FileBlock",
    "ImageBlock",
    "InvalidToolCall",
    "InvalidToolCallBlock",
    "LLMContext",
    "LLMRequest",
    "LLMToolRequest",
    "Message",
    "MessageAdapter",
    "MessageId",
    "RefusalBlock",
    "RequestId",
    "SamplingParams",
    "SystemBlock",
    "SystemMessage",
    "TextBlock",
    "ThinkingBlock",
    "ToolCall",
    "ToolCallBlock",
    "ToolCallChunk",
    "ToolResultMessage",
    "UserBlock",
    "UserMessage",
    "new_message_id",
    "new_request_id",
]

RequestId = NewType("RequestId", UUID)
"""Идентификатор запроса пользователя, проходящий через всю систему."""

MessageId = NewType("MessageId", UUID)
"""Стабильный id сообщения (для dedup, replay, referencing)."""


def new_request_id() -> RequestId:
    """Свежий RequestId."""
    return RequestId(uuid4())


def new_message_id() -> MessageId:
    """Свежий MessageId."""
    return MessageId(uuid4())


# --------------------------------------------------------------------- #
# ToolCall / InvalidToolCall
# --------------------------------------------------------------------- #


class ToolCall(BaseModel):
    """Завершённый валидный tool-call: args уже распарсены."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    name: str
    args: Mapping[str, Any]

    def args_json(self) -> str:
        """JSON-строка args для отображения/wire-форматов, требующих str."""
        return json.dumps(dict(self.args), ensure_ascii=False)


class InvalidToolCall(BaseModel):
    """Tool-call с невалидным JSON в args; пробрасывается типом, а не исключением."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    name: str
    raw_args: str
    error: str


# --------------------------------------------------------------------- #
# Content blocks (внутри Message.blocks)
# --------------------------------------------------------------------- #


class _Block(BaseModel):
    """База блока: frozen, extra=forbid, discriminator по полю `type`."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class TextBlock(_Block):
    """Текстовый блок — базовый, доступен любой роли."""

    type: Literal["text"] = "text"
    content: str


class ImageBlock(_Block):
    """Изображение для vision-моделей (URL или data URI)."""

    type: Literal["image"] = "image"
    source: str
    mime: str = "image/png"


class FileBlock(_Block):
    """Файл, загруженный через provider file API."""

    type: Literal["file"] = "file"
    file_id: str
    name: str = ""


class ToolCallBlock(_Block):
    """Tool-call от ассистента; args уже распарсены."""

    type: Literal["tool_call"] = "tool_call"
    call: ToolCall


class InvalidToolCallBlock(_Block):
    """Tool-call с невалидным JSON в args."""

    type: Literal["invalid_tool_call"] = "invalid_tool_call"
    invalid: InvalidToolCall


class ThinkingBlock(_Block):
    """Reasoning / extended thinking; signature нужен Anthropic для echo-back."""

    type: Literal["thinking"] = "thinking"
    content: str
    signature: str = ""


class RefusalBlock(_Block):
    """Модель отказалась отвечать (OpenAI refusal / Anthropic stop_reason=refusal)."""

    type: Literal["refusal"] = "refusal"
    content: str


SystemBlock: TypeAlias = TextBlock
"""Блок system-сообщения; пока только текст (cache_control блока — позже)."""

UserBlock: TypeAlias = Annotated[
    TextBlock | ImageBlock | FileBlock,
    Field(discriminator="type"),
]
"""Блок user-сообщения: text + мультимодальные."""

AssistantBlock: TypeAlias = Annotated[
    TextBlock | ToolCallBlock | InvalidToolCallBlock | ThinkingBlock | RefusalBlock,
    Field(discriminator="type"),
]
"""Блок assistant-сообщения: всё, что генерирует модель в одной генерации."""


# --------------------------------------------------------------------- #
# Message hierarchy (Pydantic discriminated union)
# --------------------------------------------------------------------- #


class Message(BaseModel, ABC):
    """База типизированного сообщения протокола; общие поля."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: MessageId = Field(default_factory=new_message_id)


class SystemMessage(Message):
    """System-prompt; список text-блоков."""

    type: Literal["system"] = "system"
    blocks: tuple[SystemBlock, ...]

    @classmethod
    def from_text(
        cls,
        content: str,
        *,
        id: MessageId | None = None,  # noqa: A002 — совпадает с полем Message.id
    ) -> SystemMessage:
        """Удобный конструктор text-only сообщения."""
        kwargs: dict[str, Any] = {"blocks": (TextBlock(content=content),)}
        if id is not None:
            kwargs["id"] = id
        return cls(**kwargs)

    @property
    def content(self) -> str:
        """Конкатенация всех TextBlock — convenience для существующих читателей."""
        return "".join(b.content for b in self.blocks if isinstance(b, TextBlock))


class UserMessage(Message):
    """Сообщение пользователя; может содержать текст + мультимодальные блоки."""

    type: Literal["user"] = "user"
    blocks: tuple[UserBlock, ...]

    @classmethod
    def from_text(
        cls,
        content: str,
        *,
        id: MessageId | None = None,  # noqa: A002 — совпадает с полем Message.id
    ) -> UserMessage:
        """Удобный конструктор text-only сообщения."""
        kwargs: dict[str, Any] = {"blocks": (TextBlock(content=content),)}
        if id is not None:
            kwargs["id"] = id
        return cls(**kwargs)

    @property
    def content(self) -> str:
        """Конкатенация всех TextBlock."""
        return "".join(b.content for b in self.blocks if isinstance(b, TextBlock))


class AssistantMessage(Message):
    """Ответ модели; упорядоченный список блоков одной генерации.

    Блоки сохраняют порядок выдачи моделью (для Anthropic extended thinking
    + tool_use это критично — подпись thinking должна echo'ься в той же
    последовательности). OpenAI Chat Completions терпит любой порядок:
    адаптер flatten'ит блоки в `content` + `tool_calls`.
    """

    type: Literal["assistant"] = "assistant"
    blocks: tuple[AssistantBlock, ...]

    @classmethod
    def from_text(
        cls,
        content: str,
        *,
        id: MessageId | None = None,  # noqa: A002 — совпадает с полем Message.id
    ) -> AssistantMessage:
        """Удобный конструктор text-only ответа (без tool_call'ов)."""
        kwargs: dict[str, Any] = {"blocks": (TextBlock(content=content),)}
        if id is not None:
            kwargs["id"] = id
        return cls(**kwargs)

    @property
    def content(self) -> str:
        """Склеенный текст всех TextBlock — convenience для существующих читателей."""
        return "".join(b.content for b in self.blocks if isinstance(b, TextBlock))

    @property
    def tool_calls(self) -> tuple[ToolCall, ...]:
        """Распакованные ToolCall из ToolCallBlock-ов в порядке появления."""
        return tuple(b.call for b in self.blocks if isinstance(b, ToolCallBlock))

    @property
    def invalid_tool_calls(self) -> tuple[InvalidToolCall, ...]:
        """Распакованные InvalidToolCall из InvalidToolCallBlock-ов."""
        return tuple(
            b.invalid for b in self.blocks if isinstance(b, InvalidToolCallBlock)
        )

    @property
    def thinking(self) -> str:
        """Склеенный текст всех ThinkingBlock."""
        return "".join(b.content for b in self.blocks if isinstance(b, ThinkingBlock))

    @property
    def refusal(self) -> str:
        """Склеенный текст всех RefusalBlock."""
        return "".join(b.content for b in self.blocks if isinstance(b, RefusalBlock))


class ToolResultMessage(Message):
    """Результат выполнения tool-call в слот id-вызова.

    Несёт доменный `ToolResult` (sealed).
    Провайдер-adapter рендерит его в свой wire-формат
    через `ToolResultVisitor[T]` при конвертации сообщения в API-параметры.
    Признак ошибки — `isinstance(result, ErrorResult)`.
    """

    type: Literal["tool_result"] = "tool_result"
    tool_call_id: str
    result: ToolResult


MessageAdapter: TypeAdapter[Message] = TypeAdapter(
    Annotated[
        SystemMessage | UserMessage | AssistantMessage | ToolResultMessage,
        Field(discriminator="type"),
    ],
)
"""
TypeAdapter для (де)сериализации Message через discriminator='type'.

Использование:
    line: str = MessageAdapter.dump_json(message).decode("utf-8")
    msg: Message = MessageAdapter.validate_json(line)
"""


DialogMessage: TypeAlias = UserMessage | AssistantMessage | ToolResultMessage
"""Сообщение диалога — всё, что не SystemMessage.

В `LLMRequest.messages` лежат только эти типы; `SystemMessage` живёт
отдельно в `LLMRequest.system_messages`, чтобы Anthropic-адаптер мог
прокидывать их в top-level `system` параметр, а OpenAI-адаптер —
префиксовать обычный список сообщений.
"""


@dataclass(frozen=True)
class ToolCallChunk:
    """
    Накопленное состояние одного tool-call в стриме
    """

    index: int
    id: str
    name: str
    args: str = ""

    def append_args(self, args_chunk: str) -> ToolCallChunk:
        """Дописать substring JSON в args."""
        return ToolCallChunk(
            index=self.index,
            id=self.id,
            name=self.name,
            args=self.args + args_chunk,
        )

    def finalize(self) -> ToolCall | InvalidToolCall:
        """Парсить args в dict; вернуть ToolCall или InvalidToolCall."""
        try:
            parsed = json.loads(self.args) if self.args else {}
        except json.JSONDecodeError as e:
            return InvalidToolCall(
                id=self.id,
                name=self.name,
                raw_args=self.args,
                error=f"invalid JSON arguments: {e}",
            )

        if not isinstance(parsed, dict):
            return InvalidToolCall(
                id=self.id,
                name=self.name,
                raw_args=self.args,
                error=f"args must be JSON object, got {type(parsed).__name__}",
            )

        return ToolCall(id=self.id, name=self.name, args=parsed)


@dataclass(frozen=True)
class AssistantMessageChunk:
    """Накопительный чанк AssistantMessage в стриме."""

    content: str = ""
    thinking: str = ""
    refusal: str = ""
    tool_call_chunks: tuple[ToolCallChunk, ...] = ()

    @classmethod
    def empty(cls) -> AssistantMessageChunk:
        return cls()

    def with_text(self, token: str) -> AssistantMessageChunk:
        """Прибавить токен в content."""
        return AssistantMessageChunk(
            content=self.content + token,
            thinking=self.thinking,
            refusal=self.refusal,
            tool_call_chunks=self.tool_call_chunks,
        )

    def with_thinking(self, token: str) -> AssistantMessageChunk:
        """Прибавить thinking-токен."""
        return AssistantMessageChunk(
            content=self.content,
            thinking=self.thinking + token,
            refusal=self.refusal,
            tool_call_chunks=self.tool_call_chunks,
        )

    def with_refusal(self, token: str) -> AssistantMessageChunk:
        """Прибавить refusal-токен."""
        return AssistantMessageChunk(
            content=self.content,
            thinking=self.thinking,
            refusal=self.refusal + token,
            tool_call_chunks=self.tool_call_chunks,
        )

    def with_tool_call_start(
        self,
        *,
        index: int,
        tool_call_id: str,
        tool_name: str,
    ) -> AssistantMessageChunk:
        """Зарегистрировать новый tool-call slot (из ToolCallStreamStarted)."""
        for existing in self.tool_call_chunks:
            if existing.index == index:
                raise LLMProtocolError(
                    f"with_tool_call_start: дубликат index={index} "
                    f"(уже зарегистрирован id={existing.id!r}, name={existing.name!r})"
                )
        return AssistantMessageChunk(
            content=self.content,
            thinking=self.thinking,
            refusal=self.refusal,
            tool_call_chunks=(
                *self.tool_call_chunks,
                ToolCallChunk(index=index, id=tool_call_id, name=tool_name),
            ),
        )

    def with_tool_call_args(
        self,
        *,
        index: int,
        args_chunk: str,
    ) -> AssistantMessageChunk:
        """Дописать args в зарегистрированный tool-call (из ToolCallArgumentDelta)."""
        updated = list(self.tool_call_chunks)
        for i, existing in enumerate(updated):
            if existing.index == index:
                updated[i] = existing.append_args(args_chunk)
                return AssistantMessageChunk(
                    content=self.content,
                    thinking=self.thinking,
                    refusal=self.refusal,
                    tool_call_chunks=tuple(updated),
                )
        raise LLMProtocolError(
            f"with_tool_call_args: index={index} не зарегистрирован — "
            f"ToolCallArgumentDelta пришла без предшествующего ToolCallStreamStarted"
        )

    def finalize(self, *, message_id: MessageId | None = None) -> AssistantMessage:
        """Замкнуть чанк в финальный AssistantMessage.

        Собирает блоки в каноническом порядке: thinking → text → tool_calls
        → invalid_tool_calls → refusal. Чанк не отслеживает interleaving
        провайдера (OpenAI Chat не interleav'ит), поэтому порядок здесь —
        конвенция. Для Anthropic streaming (block_start/delta/stop) этот
        механизм потребует доработки.
        """
        blocks: list[AssistantBlock] = []
        if self.thinking:
            blocks.append(ThinkingBlock(content=self.thinking))
        if self.content:
            blocks.append(TextBlock(content=self.content))
        for tcc in sorted(self.tool_call_chunks, key=lambda c: c.index):
            tc = tcc.finalize()
            if isinstance(tc, ToolCall):
                blocks.append(ToolCallBlock(call=tc))
            else:
                blocks.append(InvalidToolCallBlock(invalid=tc))
        if self.refusal:
            blocks.append(RefusalBlock(content=self.refusal))

        kwargs: dict[str, Any] = {"blocks": tuple(blocks)}
        if message_id is not None:
            kwargs["id"] = message_id
        return AssistantMessage(**kwargs)

    def is_empty(self) -> bool:
        """True если ни текста, ни thinking, ни refusal, ни tool-call'ов."""
        return (
            not self.content
            and not self.thinking
            and not self.refusal
            and not self.tool_call_chunks
        )


@dataclass(frozen=True)
class SamplingParams:
    temperature: float | None = None
    top_p: float | None = None
    max_tokens: int | None = None
    seed: int | None = None
    stop: tuple[str, ...] | None = None
    frequency_penalty: float | None = None
    presence_penalty: float | None = None


@dataclass(frozen=True)
class LLMToolRequest:
    tools: tuple[ToolSchema, ...] = ()
    tool_choice: str | None = None
    parallel_tool_calls: bool | None = None


@dataclass(frozen=True)
class LLMRequest:
    request_id: RequestId
    model: str
    system_messages: tuple[SystemMessage, ...] = ()
    messages: tuple[DialogMessage, ...] = ()
    tools: LLMToolRequest = field(default_factory=LLMToolRequest)
    sampling: SamplingParams = field(default_factory=SamplingParams)
    response_format: Mapping[str, Any] | None = None

    def has_tools(self) -> bool:
        return bool(self.tools.tools)


@dataclass(frozen=True)
class LLMContext:
    request: LLMRequest
