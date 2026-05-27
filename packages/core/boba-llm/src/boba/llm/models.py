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

from boba.tools.domain import ToolSchema

__all__ = [
    "AssistantMessage",
    "AssistantMessageChunk",
    "DialogMessage",
    "InvalidToolCall",
    "LLMContext",
    "LLMRequest",
    "LLMToolDefinition",
    "Message",
    "MessageAdapter",
    "MessageId",
    "RequestId",
    "SamplingParams",
    "SystemMessage",
    "ToolCall",
    "ToolResultMessage",
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
    """System-prompt (plain text)."""

    type: Literal["system"] = "system"
    content: str

    @classmethod
    def from_text(cls, content: str) -> SystemMessage:
        return cls(content=content)


class UserMessage(Message):
    """Сообщение пользователя (plain text)."""

    type: Literal["user"] = "user"
    content: str

    @classmethod
    def from_text(cls, content: str) -> UserMessage:
        return cls(content=content)


class AssistantMessage(Message):
    """Ответ модели одной генерации: текст + reasoning + refusal + tool-call'ы.

    Плоские поля вместо упорядоченных блоков: OpenAI Chat всё равно flatten'ит
    (`content` + `tool_calls`), а thinking/refusal/invalid_tool_calls живут в
    домене для replay/audit и провайдеру не отправляются.
    """

    type: Literal["assistant"] = "assistant"
    content: str = ""
    thinking: str = ""
    refusal: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    invalid_tool_calls: tuple[InvalidToolCall, ...] = ()

    @classmethod
    def from_text(cls, content: str) -> AssistantMessage:
        return cls(content=content)

    def is_empty(self) -> bool:
        """True если модель не выдала никакого контента."""
        return not (
            self.content
            or self.thinking
            or self.refusal
            or self.tool_calls
            or self.invalid_tool_calls
        )


class ToolResultMessage(Message):
    """Результат выполнения tool-call (plain text).

    Конвертация из доменного `ToolResult` — `tool_result_to_message(...)`
    (`boba.llm.tool_result_render`), чтобы models.py не знал про варианты
    ToolResult.
    """

    type: Literal["tool_result"] = "tool_result"
    tool_call_id: str
    content: str
    is_error: bool = False


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
class AssistantMessageChunk:
    """Аккумулятор AssistantMessage в стриме.

    Текстовые поля (text/thinking/refusal) копятся посимвольно в StringIO;
    tool-call'ы добавляются уже декодированными (`add_tool_call`) — их декод из
    wire-формата (строка→args, валидность) делает провайдер.
    """

    _text: io.StringIO = field(default_factory=io.StringIO)
    _thinking: io.StringIO = field(default_factory=io.StringIO)
    _refusal: io.StringIO = field(default_factory=io.StringIO)
    _tool_calls: list[ToolCall] = field(default_factory=list)
    _invalid_tool_calls: list[InvalidToolCall] = field(default_factory=list)

    @classmethod
    def empty(cls) -> AssistantMessageChunk:
        return cls()

    def append_text(self, token: str) -> None:
        self._text.write(token)

    def append_thinking(self, token: str) -> None:
        self._thinking.write(token)

    def append_refusal(self, token: str) -> None:
        self._refusal.write(token)

    @property
    def thinking(self) -> str:
        """Накопленный reasoning на текущий момент."""
        return self._thinking.getvalue()

    @property
    def text(self) -> str:
        """Накопленный текстовый ответ на текущий момент."""
        return self._text.getvalue()

    @property
    def refusal(self) -> str:
        """Накопленный refusal на текущий момент."""
        return self._refusal.getvalue()

    def add_tool_call(self, call: ToolCall | InvalidToolCall) -> None:
        """Добавить уже декодированный tool-call (args распарсил провайдер)."""
        if isinstance(call, ToolCall):
            self._tool_calls.append(call)
        else:
            self._invalid_tool_calls.append(call)

    def finalize(self) -> AssistantMessage:
        """Замкнуть чанк в финальный AssistantMessage со свежим id."""
        return AssistantMessage(
            content=self._text.getvalue(),
            thinking=self._thinking.getvalue(),
            refusal=self._refusal.getvalue(),
            tool_calls=tuple(self._tool_calls),
            invalid_tool_calls=tuple(self._invalid_tool_calls),
        )

    def is_empty(self) -> bool:
        """True если ничего не накоплено."""
        return not (
            self._text.getvalue()
            or self._thinking.getvalue()
            or self._refusal.getvalue()
            or self._tool_calls
            or self._invalid_tool_calls
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
