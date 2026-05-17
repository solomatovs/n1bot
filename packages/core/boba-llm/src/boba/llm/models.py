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
from boba.tools.domain import (
    ErrorResult,
    JsonResult,
    TextResult,
    ToolResult,
    ToolSchema,
)

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


class SystemMessage(Message):
    """System-prompt; список text-блоков."""

    type: Literal["system"] = "system"
    blocks: tuple[SystemBlock, ...]

    @classmethod
    def from_text(cls, content: str) -> SystemMessage:
        """Text-only сообщение со свежим id."""
        return cls(id=new_message_id(), blocks=(TextBlock(content=content),))

    @classmethod
    def from_text_with_id(
        cls,
        content: str,
        *,
        id: MessageId,  # noqa: A002 — совпадает с полем Message.id
    ) -> SystemMessage:
        """Text-only сообщение с заданным id (replay/тесты)."""
        return cls(id=id, blocks=(TextBlock(content=content),))

    @property
    def content(self) -> str:
        """Конкатенация всех TextBlock — convenience для существующих читателей."""
        return "".join(b.content for b in self.blocks if isinstance(b, TextBlock))


class UserMessage(Message):
    """Сообщение пользователя; может содержать текст + мультимодальные блоки."""

    type: Literal["user"] = "user"
    blocks: tuple[UserBlock, ...]

    @classmethod
    def from_text(cls, content: str) -> UserMessage:
        """Text-only сообщение со свежим id."""
        return cls(id=new_message_id(), blocks=(TextBlock(content=content),))

    @classmethod
    def from_text_with_id(
        cls,
        content: str,
        *,
        id: MessageId,  # noqa: A002 — совпадает с полем Message.id
    ) -> UserMessage:
        """Text-only сообщение с заданным id (replay/тесты)."""
        return cls(id=id, blocks=(TextBlock(content=content),))

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
    def from_text(cls, content: str) -> AssistantMessage:
        """Text-only ответ (без tool_call'ов) со свежим id."""
        return cls(id=new_message_id(), blocks=(TextBlock(content=content),))

    @classmethod
    def from_text_with_id(
        cls,
        content: str,
        *,
        id: MessageId,  # noqa: A002 — совпадает с полем Message.id
    ) -> AssistantMessage:
        """Text-only ответ с заданным id (replay/тесты)."""
        return cls(id=id, blocks=(TextBlock(content=content),))

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
    """
    Результат выполнения tool-call в слот id-вызова — block-based.

    Содержимое — упорядоченные блоки (text + image) совместимо с Anthropic
    multi-block tool_result. `is_error: True` сигнализирует провайдеру об
    отказе tool'а (Anthropic выставляет одноимённый wire-флаг; OpenAI Chat
    его игнорирует, ошибка попадает в content).

    Конструктор `from_result(...)` принимает доменный `ToolResult` и
    конвертирует в blocks — это граница между tool-execution domain и
    wire-message domain. Прямой `ToolResultMessage(blocks=..., is_error=...)`
    остаётся для случаев, когда блоки строятся независимо (replay, image-tool).
    """

    type: Literal["tool_result"] = "tool_result"
    tool_call_id: str
    blocks: tuple[ToolResultContentBlock, ...]
    is_error: bool = False

    @staticmethod
    def _result_to_blocks(
        result: ToolResult,
    ) -> tuple[tuple[ToolResultContentBlock, ...], bool]:
        """
        Граница tools-domain ↔ wire-message-domain: `ToolResult` → blocks + is_error
        """

        match result:
            case TextResult(text=t):
                return (TextBlock(content=t),), False
            case JsonResult(payload=p):
                return (TextBlock(content=json.dumps(p, ensure_ascii=False)),), False
            case ErrorResult(message=m):
                return (TextBlock(content=m),), True

    @classmethod
    def from_result(
        cls,
        *,
        tool_call_id: str,
        result: ToolResult,
    ) -> ToolResultMessage:
        """Convert доменный `ToolResult` → blocks + is_error со свежим id.

        Граница tools-domain ↔ wire-message-domain: tool продолжают
        возвращать `ToolResult`, а сообщение хранит уже block-форму.
        Pyright проверяет exhaustiveness через discriminator `kind`.
        """
        blocks, is_error = cls._result_to_blocks(result)
        return cls(
            id=new_message_id(),
            tool_call_id=tool_call_id,
            blocks=blocks,
            is_error=is_error,
        )

    @classmethod
    def from_result_with_id(
        cls,
        *,
        tool_call_id: str,
        result: ToolResult,
        id: MessageId,  # noqa: A002 — совпадает с полем Message.id
    ) -> ToolResultMessage:
        """Тот же `from_result`, но с заданным id (replay/тесты)."""
        blocks, is_error = cls._result_to_blocks(result)
        return cls(
            id=id,
            tool_call_id=tool_call_id,
            blocks=blocks,
            is_error=is_error,
        )


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
class PartialTextBlock:
    """Частично собранный TextBlock в стриме."""

    type: Literal["text"] = "text"
    content: str = ""

    def with_token(self, token: str) -> PartialTextBlock:
        return PartialTextBlock(content=self.content + token)

    def finalize(self) -> TextBlock:
        return TextBlock(content=self.content)


@dataclass(frozen=True)
class PartialThinkingBlock:
    """Частично собранный ThinkingBlock в стриме."""

    type: Literal["thinking"] = "thinking"
    content: str = ""
    signature: str = ""

    def with_token(self, token: str) -> PartialThinkingBlock:
        return PartialThinkingBlock(
            content=self.content + token,
            signature=self.signature,
        )

    def with_signature(self, signature: str) -> PartialThinkingBlock:
        return PartialThinkingBlock(content=self.content, signature=signature)

    def finalize(self) -> ThinkingBlock:
        return ThinkingBlock(content=self.content, signature=self.signature)


@dataclass(frozen=True)
class PartialRefusalBlock:
    """Частично собранный RefusalBlock в стриме."""

    type: Literal["refusal"] = "refusal"
    content: str = ""

    def with_token(self, token: str) -> PartialRefusalBlock:
        return PartialRefusalBlock(content=self.content + token)

    def finalize(self) -> RefusalBlock:
        return RefusalBlock(content=self.content)


@dataclass(frozen=True)
class PartialToolCallBlock:
    """Частично собранный tool-call: args накапливаются как сырая JSON-строка."""

    type: Literal["tool_call"] = "tool_call"
    index: int = 0
    id: str = ""
    name: str = ""
    args: str = ""

    def with_args(self, args_chunk: str) -> PartialToolCallBlock:
        return PartialToolCallBlock(
            index=self.index,
            id=self.id,
            name=self.name,
            args=self.args + args_chunk,
        )

    def finalize(self) -> ToolCallBlock | InvalidToolCallBlock:
        """Парсить args в dict; вернуть ToolCallBlock или InvalidToolCallBlock."""
        try:
            parsed = json.loads(self.args) if self.args else {}
        except json.JSONDecodeError as e:
            return InvalidToolCallBlock(
                invalid=InvalidToolCall(
                    id=self.id,
                    name=self.name,
                    raw_args=self.args,
                    error=f"invalid JSON arguments: {e}",
                ),
            )
        if not isinstance(parsed, dict):
            return InvalidToolCallBlock(
                invalid=InvalidToolCall(
                    id=self.id,
                    name=self.name,
                    raw_args=self.args,
                    error=f"args must be JSON object, got {type(parsed).__name__}",
                ),
            )
        return ToolCallBlock(call=ToolCall(id=self.id, name=self.name, args=parsed))


PartialAssistantBlock: TypeAlias = (
    PartialTextBlock | PartialThinkingBlock | PartialRefusalBlock | PartialToolCallBlock
)
"""Растущий-в-стриме блок ассистента."""


@dataclass(frozen=True)
class AssistantMessageChunk:
    """
    Накопительный чанк AssistantMessage в стриме как ordered-list partial-блоков.

    Token-style события (text/thinking/refusal) — находят соответствующий
    тип блока (один блок-на-тип для OpenAI Chat, где нет явных границ) либо
    создают новый, если такого ещё нет. Tool-call события явно адресуют
    блок по `index`. Финализация: каждый partial → финальный AssistantBlock
    в порядке хранения.

    Когда подключим Anthropic streaming с явными `content_block_start/delta/
    stop` событиями, методы можно расширить block-index'ом для произвольного
    interleaving (text → tool_call → text) — структура уже готова.
    """

    blocks: tuple[PartialAssistantBlock, ...] = ()

    @classmethod
    def empty(cls) -> AssistantMessageChunk:
        return cls()

    def with_text(self, token: str) -> AssistantMessageChunk:
        """Прибавить text-токен (find-or-create PartialTextBlock)."""
        return self._append_token(PartialTextBlock, token)

    def with_thinking(self, token: str) -> AssistantMessageChunk:
        """Прибавить thinking-токен (find-or-create PartialThinkingBlock)."""
        return self._append_token(PartialThinkingBlock, token)

    def with_refusal(self, token: str) -> AssistantMessageChunk:
        """Прибавить refusal-токен (find-or-create PartialRefusalBlock)."""
        return self._append_token(PartialRefusalBlock, token)

    def _append_token(
        self,
        block_cls: type[PartialTextBlock]
        | type[PartialThinkingBlock]
        | type[PartialRefusalBlock],
        token: str,
    ) -> AssistantMessageChunk:
        for i, b in enumerate(self.blocks):
            if isinstance(b, block_cls):
                updated = b.with_token(token)
                return AssistantMessageChunk(
                    blocks=(*self.blocks[:i], updated, *self.blocks[i + 1 :]),
                )
        return AssistantMessageChunk(
            blocks=(*self.blocks, block_cls().with_token(token)),
        )

    def with_tool_call_start(
        self,
        *,
        index: int,
        tool_call_id: str,
        tool_name: str,
    ) -> AssistantMessageChunk:
        """Зарегистрировать новый tool-call slot (из ToolCallStreamStarted)."""
        for b in self.blocks:
            if isinstance(b, PartialToolCallBlock) and b.index == index:
                raise LLMProtocolError(
                    f"with_tool_call_start: дубликат index={index} "
                    f"(уже зарегистрирован id={b.id!r}, name={b.name!r})"
                )
        return AssistantMessageChunk(
            blocks=(
                *self.blocks,
                PartialToolCallBlock(index=index, id=tool_call_id, name=tool_name),
            ),
        )

    def with_tool_call_args(
        self,
        *,
        index: int,
        args_chunk: str,
    ) -> AssistantMessageChunk:
        """Дописать args в зарегистрированный tool-call (из ToolCallArgumentDelta)."""
        for i, b in enumerate(self.blocks):
            if isinstance(b, PartialToolCallBlock) and b.index == index:
                updated = b.with_args(args_chunk)
                return AssistantMessageChunk(
                    blocks=(*self.blocks[:i], updated, *self.blocks[i + 1 :]),
                )
        raise LLMProtocolError(
            f"with_tool_call_args: index={index} не зарегистрирован — "
            f"ToolCallArgumentDelta пришла без предшествующего ToolCallStreamStarted"
        )

    def finalize(self) -> AssistantMessage:
        """Замкнуть чанк в финальный AssistantMessage со свежим id."""
        return AssistantMessage(
            id=new_message_id(),
            blocks=tuple(b.finalize() for b in self.blocks),
        )

    def finalize_with_id(self, *, message_id: MessageId) -> AssistantMessage:
        """Тот же `finalize`, но с заданным id (replay/тесты)."""
        return AssistantMessage(
            id=message_id,
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
