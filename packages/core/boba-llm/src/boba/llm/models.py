"""Модели LLM-протокола: типизированные сообщения, tool-calls, request/context."""

from __future__ import annotations

import io
import json
from abc import ABC
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Annotated, Any, Literal, NewType, Self, TypeAlias
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from boba.llm.errors import LLMProtocolError
from boba.tools.domain import ToolSchema

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
    "LLMToolDefinition",
    "Message",
    "MessageAdapter",
    "MessageId",
    "PartialAssistantBlock",
    "PartialRefusalBlock",
    "PartialTextBlock",
    "PartialThinkingBlock",
    "PartialToolCallBlock",
    "RefusalBlock",
    "RequestId",
    "SamplingParams",
    "SystemBlock",
    "SystemMessage",
    "TextBlock",
    "ThinkingBlock",
    "ToolCall",
    "ToolCallBlock",
    "ToolResultContentBlock",
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
    """Reasoning / extended thinking"""

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

ToolResultContentBlock: TypeAlias = Annotated[
    TextBlock | ImageBlock,
    Field(discriminator="type"),
]
"""Блок содержимого tool-result: текст или изображение (Anthropic-совместимо)."""


# --------------------------------------------------------------------- #
# Message hierarchy (Pydantic discriminated union)
# --------------------------------------------------------------------- #


class Message(BaseModel, ABC):
    """База типизированного сообщения протокола; общие поля."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: MessageId = Field(default_factory=new_message_id)

    def set_id(self, id: MessageId) -> Self:  # noqa: A002 — совпадает с полем
        """
        Копия сообщения с заменённым id (для replay/тестов).
        Используется model_copy потому что модель уже frozen
        поэтому изменение невозможно
        """
        return self.model_copy(update={"id": id})


class SystemMessage(Message):
    """System-prompt; список text-блоков."""

    type: Literal["system"] = "system"
    blocks: tuple[SystemBlock, ...]

    @classmethod
    def from_text(cls, content: str) -> SystemMessage:
        """Text-only сообщение со свежим id.

        Для replay/тестов с заданным id: `SystemMessage.from_text(...).set_id(my_id)`.
        """
        return cls(id=new_message_id(), blocks=(TextBlock(content=content),))

    @property
    def text_blocks(self) -> tuple[TextBlock, ...]:
        """Только text-блоки (всегда все, других в SystemBlock нет)."""
        return tuple(b for b in self.blocks if isinstance(b, TextBlock))

    @property
    def content(self) -> str:
        """Конкатенация всех TextBlock — convenience для существующих читателей."""
        return "".join(b.content for b in self.text_blocks)


class UserMessage(Message):
    """Сообщение пользователя; может содержать текст + мультимодальные блоки."""

    type: Literal["user"] = "user"
    blocks: tuple[UserBlock, ...]

    @classmethod
    def from_text(cls, content: str) -> UserMessage:
        """Text-only сообщение со свежим id.

        Для replay/тестов с заданным id: `UserMessage.from_text(...).set_id(my_id)`.
        """
        return cls(id=new_message_id(), blocks=(TextBlock(content=content),))

    @property
    def text_blocks(self) -> tuple[TextBlock, ...]:
        return tuple(b for b in self.blocks if isinstance(b, TextBlock))

    @property
    def image_blocks(self) -> tuple[ImageBlock, ...]:
        return tuple(b for b in self.blocks if isinstance(b, ImageBlock))

    @property
    def file_blocks(self) -> tuple[FileBlock, ...]:
        return tuple(b for b in self.blocks if isinstance(b, FileBlock))

    @property
    def content(self) -> str:
        """Конкатенация всех TextBlock."""
        return "".join(b.content for b in self.text_blocks)


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
    def from_text(cls, content: str) -> AssistantMessage:
        """Text-only ответ (без tool_call'ов) со свежим id.

        Для replay/тестов: `AssistantMessage.from_text(...).set_id(my_id)`.
        """
        return cls(id=new_message_id(), blocks=(TextBlock(content=content),))

    @property
    def text_blocks(self) -> tuple[TextBlock, ...]:
        return tuple(b for b in self.blocks if isinstance(b, TextBlock))

    @property
    def tool_call_blocks(self) -> tuple[ToolCallBlock, ...]:
        return tuple(b for b in self.blocks if isinstance(b, ToolCallBlock))

    @property
    def invalid_tool_call_blocks(self) -> tuple[InvalidToolCallBlock, ...]:
        return tuple(b for b in self.blocks if isinstance(b, InvalidToolCallBlock))

    @property
    def thinking_blocks(self) -> tuple[ThinkingBlock, ...]:
        return tuple(b for b in self.blocks if isinstance(b, ThinkingBlock))

    @property
    def refusal_blocks(self) -> tuple[RefusalBlock, ...]:
        return tuple(b for b in self.blocks if isinstance(b, RefusalBlock))

    @property
    def content(self) -> str:
        """Склеенный текст всех TextBlock — convenience для существующих читателей."""
        return "".join(b.content for b in self.text_blocks)

    @property
    def tool_calls(self) -> tuple[ToolCall, ...]:
        """Распакованные ToolCall из ToolCallBlock-ов в порядке появления."""
        return tuple(b.call for b in self.tool_call_blocks)

    @property
    def invalid_tool_calls(self) -> tuple[InvalidToolCall, ...]:
        """Распакованные InvalidToolCall из InvalidToolCallBlock-ов."""
        return tuple(b.invalid for b in self.invalid_tool_call_blocks)

    @property
    def thinking(self) -> str:
        """Склеенный текст всех ThinkingBlock."""
        return "".join(b.content for b in self.thinking_blocks)

    @property
    def refusal(self) -> str:
        """Склеенный текст всех RefusalBlock."""
        return "".join(b.content for b in self.refusal_blocks)


class ToolResultMessage(Message):
    """
    Результат выполнения tool-call

    Содержимое — упорядоченные блоки (text + image). Для конвертации из
    доменного `ToolResult` (tools-domain) → `ToolResultMessage` используй
    `boba.llm.tool_result_render.tool_result_to_message(...)` — он живёт
    отдельным модулем, чтобы `models.py` не знал про варианты ToolResult.
    """

    type: Literal["tool_result"] = "tool_result"
    tool_call_id: str
    blocks: tuple[ToolResultContentBlock, ...]
    is_error: bool = False

    @property
    def text_blocks(self) -> tuple[TextBlock, ...]:
        return tuple(b for b in self.blocks if isinstance(b, TextBlock))

    @property
    def image_blocks(self) -> tuple[ImageBlock, ...]:
        return tuple(b for b in self.blocks if isinstance(b, ImageBlock))

    @property
    def content(self) -> str:
        """Конкатенация всех TextBlock — для OpenAI-Chat-style плоского tool-content."""
        return "".join(b.content for b in self.text_blocks)


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


@dataclass
class PartialTextBlock:
    """Mutable buffer для растущего TextBlock в стриме."""

    content: io.StringIO = field(default_factory=io.StringIO)

    def append_token(self, token: str) -> None:
        self.content.write(token)

    def finalize(self) -> TextBlock:
        return TextBlock(content=self.content.getvalue())


@dataclass
class PartialThinkingBlock:
    """Mutable buffer для растущего ThinkingBlock в стриме."""

    content: io.StringIO = field(default_factory=io.StringIO)
    signature: str = ""

    def append_token(self, token: str) -> None:
        self.content.write(token)

    def set_signature(self, signature: str) -> None:
        self.signature = signature

    def finalize(self) -> ThinkingBlock:
        return ThinkingBlock(
            content=self.content.getvalue(),
            signature=self.signature,
        )


@dataclass
class PartialRefusalBlock:
    """Mutable buffer для растущего RefusalBlock в стриме."""

    content: io.StringIO = field(default_factory=io.StringIO)

    def append_token(self, token: str) -> None:
        self.content.write(token)

    def finalize(self) -> RefusalBlock:
        return RefusalBlock(content=self.content.getvalue())


@dataclass
class PartialToolCallBlock:
    """Mutable buffer для растущего tool-call.

    `args` накапливаются как сырая JSON-строка (фрагменты пишутся в StringIO).
    """

    index: int
    id: str
    name: str
    args: io.StringIO = field(default_factory=io.StringIO)

    def append_args(self, args_chunk: str) -> None:
        self.args.write(args_chunk)

    def finalize(self) -> ToolCallBlock | InvalidToolCallBlock:
        """Парсить args в dict; вернуть ToolCallBlock или InvalidToolCallBlock."""
        raw = self.args.getvalue()
        try:
            parsed = json.loads(raw) if raw else {}
        except json.JSONDecodeError as e:
            return InvalidToolCallBlock(
                invalid=InvalidToolCall(
                    id=self.id,
                    name=self.name,
                    raw_args=raw,
                    error=f"invalid JSON arguments: {e}",
                ),
            )

        if not isinstance(parsed, dict):
            return InvalidToolCallBlock(
                invalid=InvalidToolCall(
                    id=self.id,
                    name=self.name,
                    raw_args=raw,
                    error=f"args must be JSON object, got {type(parsed).__name__}",
                ),
            )

        return ToolCallBlock(call=ToolCall(id=self.id, name=self.name, args=parsed))


PartialAssistantBlock: TypeAlias = (
    PartialTextBlock | PartialThinkingBlock | PartialRefusalBlock | PartialToolCallBlock
)
"""Растущий-в-стриме блок ассистента."""

@dataclass
class AssistantMessageChunk:
    """
    аккумулятор AssistantMessage
    blocks содержит список из разных типов блоков
    """

    blocks: list[PartialAssistantBlock] = field(default_factory=list)

    @classmethod
    def empty(cls) -> AssistantMessageChunk:
        return cls()

    def append_text(self, token: str) -> None:
        """Прибавить text-токен в существующий PartialTextBlock или создать новый."""
        for b in self.blocks:
            if isinstance(b, PartialTextBlock):
                b.append_token(token)
                return
        block = PartialTextBlock()
        block.append_token(token)
        self.blocks.append(block)

    def append_thinking(self, token: str) -> None:
        """Прибавить thinking-токен в существующий PartialThinkingBlock или создать."""
        for b in self.blocks:
            if isinstance(b, PartialThinkingBlock):
                b.append_token(token)
                return
        block = PartialThinkingBlock()
        block.append_token(token)
        self.blocks.append(block)

    def append_refusal(self, token: str) -> None:
        """Прибавить refusal-токен в существующий PartialRefusalBlock или создать."""
        for b in self.blocks:
            if isinstance(b, PartialRefusalBlock):
                b.append_token(token)
                return
        block = PartialRefusalBlock()
        block.append_token(token)
        self.blocks.append(block)

    def start_tool_call(
        self,
        *,
        index: int,
        tool_call_id: str,
        tool_name: str,
    ) -> None:
        """Зарегистрировать новый tool-call slot (из ToolCallStreamStarted)."""
        for b in self.blocks:
            if isinstance(b, PartialToolCallBlock) and b.index == index:
                raise LLMProtocolError(
                    f"start_tool_call: дубликат index={index} "
                    f"(уже зарегистрирован id={b.id!r}, name={b.name!r})"
                )
        self.blocks.append(
            PartialToolCallBlock(index=index, id=tool_call_id, name=tool_name),
        )

    def append_tool_call_args(self, *, index: int, args_chunk: str) -> None:
        """Дописать args в зарегистрированный tool-call (из ToolCallArgumentDelta)."""
        for b in self.blocks:
            if isinstance(b, PartialToolCallBlock) and b.index == index:
                b.append_args(args_chunk)
                return
        raise LLMProtocolError(
            f"append_tool_call_args: index={index} не зарегистрирован — "
            f"ToolCallArgumentDelta пришла без предшествующего ToolCallStreamStarted"
        )

    def finalize(self) -> AssistantMessage:
        """Замкнуть чанк в финальный AssistantMessage со свежим id.

        Для replay/тестов: `chunk.finalize().set_id(my_id)`.
        """
        return AssistantMessage(
            id=new_message_id(),
            blocks=tuple(b.finalize() for b in self.blocks),
        )

    def is_empty(self) -> bool:
        """True если ни одного partial-блока не накоплено."""
        return not self.blocks


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
class LLMToolDefinition:
    tools: tuple[ToolSchema, ...] = ()
    tool_choice: str | None = None
    parallel_tool_calls: bool | None = None


@dataclass(frozen=True)
class LLMRequest:
    request_id: RequestId
    model: str
    system_messages: tuple[SystemMessage, ...] = ()
    dialog_messages: tuple[DialogMessage, ...] = ()
    tools_definition: LLMToolDefinition = field(default_factory=LLMToolDefinition)
    sampling: SamplingParams = field(default_factory=SamplingParams)
    response_format: Mapping[str, Any] | None = None
    stream: bool = True

    def has_tools(self) -> bool:
        return bool(self.tools_definition.tools)


@dataclass(frozen=True)
class LLMContext:
    request: LLMRequest
