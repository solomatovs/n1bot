from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

from boba.patterns import UuId

LLMRole = Literal["system", "user", "assistant", "tool"]


class RequestId(UuId):
    """Идентификатор запроса пользователя, проходящий через всю систему."""


@dataclass(frozen=True)
class LLMToolCall:
    id: str
    name: str
    arguments: str


@dataclass(frozen=True)
class LLMMessage:
    role: LLMRole
    content: str
    tool_call_id: str | None = None
    tool_calls: tuple[LLMToolCall, ...] = ()


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
    system_message: LLMMessage
    messages: tuple[LLMMessage, ...] = ()
    tools: LLMToolRequest = field(default_factory=LLMToolRequest)
    sampling: SamplingParams = field(default_factory=SamplingParams)
    response_format: Mapping[str, Any] | None = None

    def messages_count(self) -> int:
        """Всего сообщений в запросе."""
        return len(self.messages)

    def has_tools(self) -> bool:
        return bool(self.tools.tools)


@dataclass
class LLMContext:
    request: LLMRequest
    request_id: RequestId
    attempt: int = 0
