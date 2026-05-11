"""Модели LLM-протокола: типизированные сообщения, tool-calls, request/context."""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from boba.llm.errors import LLMProtocolError
from boba.patterns import UuId
from boba.tools.domain import ToolResult

__all__ = [
    "AssistantMessage",
    "AssistantMessageChunk",
    "InvalidToolCall",
    "LLMContext",
    "LLMRequest",
    "LLMToolRequest",
    "LLMToolSchema",
    "Message",
    "MessageId",
    "RequestId",
    "SamplingParams",
    "SystemMessage",
    "ToolCall",
    "ToolCallChunk",
    "ToolResultMessage",
    "UserMessage",
]


class RequestId(UuId):
    """Идентификатор запроса пользователя, проходящий через всю систему."""


class MessageId(UuId):
    """Стабильный id сообщения (для dedup, replay, referencing)."""


@dataclass(frozen=True)
class ToolCall:
    """Завершённый валидный tool-call: args уже распарсены."""

    id: str
    name: str
    args: Mapping[str, Any]

    def args_json(self) -> str:
        """JSON-строка args для отображения/wire-форматов, требующих str."""
        return json.dumps(dict(self.args), ensure_ascii=False)


@dataclass(frozen=True)
class InvalidToolCall:
    """Tool-call с невалидным JSON в args; пробрасывается типом, а не исключением."""

    id: str
    name: str
    raw_args: str
    error: str


@dataclass(frozen=True, kw_only=True)
class Message:
    """База типизированного сообщения протокола; общие поля."""

    id: MessageId = field(default_factory=MessageId.new)


@dataclass(frozen=True, kw_only=True)
class SystemMessage(Message):
    """System-prompt; всегда text-only."""

    content: str


@dataclass(frozen=True, kw_only=True)
class UserMessage(Message):
    """Сообщение пользователя (вход или синтетический critique от агента)."""

    content: str


@dataclass(frozen=True, kw_only=True)
class AssistantMessage(Message):
    """Ответ модели; text + tool_calls + invalid_tool_calls."""

    content: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    invalid_tool_calls: tuple[InvalidToolCall, ...] = ()


@dataclass(frozen=True, kw_only=True)
class ToolResultMessage(Message):
    """Результат выполнения tool-call в слот id-вызова.

    Несёт доменный `ToolResult` (sealed). Провайдер-adapter рендерит его
    в свой wire-формат через `ToolResultVisitor[T]` при конвертации
    сообщения в API-параметры. Признак ошибки — `isinstance(result, ErrorResult)`.
    """

    tool_call_id: str
    result: ToolResult


@dataclass(frozen=True)
class ToolCallChunk:
    """Накопленное состояние одного tool-call'а в стриме (args — substring JSON)."""

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
    tool_call_chunks: tuple[ToolCallChunk, ...] = ()

    @classmethod
    def empty(cls) -> AssistantMessageChunk:
        return cls()

    def with_text(self, token: str) -> AssistantMessageChunk:
        """Прибавить токен в content."""
        return AssistantMessageChunk(
            content=self.content + token,
            thinking=self.thinking,
            tool_call_chunks=self.tool_call_chunks,
        )

    def with_thinking(self, token: str) -> AssistantMessageChunk:
        """Прибавить thinking-токен."""
        return AssistantMessageChunk(
            content=self.content,
            thinking=self.thinking + token,
            tool_call_chunks=self.tool_call_chunks,
        )

    def with_tool_call_start(
        self, *, index: int, tool_call_id: str, tool_name: str,
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
            tool_call_chunks=(
                *self.tool_call_chunks,
                ToolCallChunk(index=index, id=tool_call_id, name=tool_name),
            ),
        )

    def with_tool_call_args(
        self, *, index: int, args_chunk: str,
    ) -> AssistantMessageChunk:
        """Дописать args в зарегистрированный tool-call (из ToolCallArgumentDelta)."""
        updated = list(self.tool_call_chunks)
        for i, existing in enumerate(updated):
            if existing.index == index:
                updated[i] = existing.append_args(args_chunk)
                return AssistantMessageChunk(
                    content=self.content,
                    thinking=self.thinking,
                    tool_call_chunks=tuple(updated),
                )
        raise LLMProtocolError(
            f"with_tool_call_args: index={index} не зарегистрирован — "
            f"ToolCallArgumentDelta пришла без предшествующего ToolCallStreamStarted"
        )

    def finalize(self, *, message_id: MessageId | None = None) -> AssistantMessage:
        """Замкнуть чанк в финальный AssistantMessage; tool-call'ы парсятся."""
        valid: list[ToolCall] = []
        invalid: list[InvalidToolCall] = []
        for tcc in sorted(self.tool_call_chunks, key=lambda c: c.index):
            result = tcc.finalize()
            if isinstance(result, ToolCall):
                valid.append(result)
            else:
                invalid.append(result)
        kwargs: dict[str, Any] = {
            "content": self.content,
            "tool_calls": tuple(valid),
            "invalid_tool_calls": tuple(invalid),
        }
        if message_id is not None:
            kwargs["id"] = message_id
        return AssistantMessage(**kwargs)

    def is_empty(self) -> bool:
        """True если ни текста, ни thinking, ни tool-call'ов."""
        return (
            not self.content
            and not self.thinking
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
class LLMToolSchema:
    """Декларация тула для LLM-провайдера: имя, описание, JSON-schema."""

    name: str
    description: str
    parameters_schema: Mapping[str, Any]


@dataclass(frozen=True)
class LLMToolRequest:
    tools: tuple[LLMToolSchema, ...] = ()
    tool_choice: str | None = None
    parallel_tool_calls: bool | None = None


@dataclass(frozen=True)
class LLMRequest:
    model: str
    system_message: SystemMessage
    messages: tuple[Message, ...] = ()
    tools: LLMToolRequest = field(default_factory=LLMToolRequest)
    sampling: SamplingParams = field(default_factory=SamplingParams)
    response_format: Mapping[str, Any] | None = None

    def messages_count(self) -> int:
        """Всего сообщений в запросе (без system)."""
        return len(self.messages)

    def has_tools(self) -> bool:
        return bool(self.tools.tools)

    def all_messages(self) -> Iterable[Message]:
        """Итератор: system + остальные сообщения в порядке отправки."""
        yield self.system_message
        yield from self.messages


@dataclass(frozen=True)
class LLMContext:
    request: LLMRequest
    request_id: RequestId
