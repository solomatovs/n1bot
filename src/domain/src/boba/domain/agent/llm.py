"""Порт LLM-клиента. Domain не знает про litellm/openai — только этот контракт."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Iterator


@dataclass(frozen=True)
class LLMToolCall:
    """Tool call, пришедший от LLM."""

    id: str
    name: str
    arguments: str


@dataclass(frozen=True)
class LLMDelta:
    """Один chunk стриминга от LLM."""

    thinking: str | None = None
    content: str | None = None
    tool_calls: list[LLMToolCall] = field(default_factory=list)


@dataclass(frozen=True)
class LLMMessage:
    """Одно сообщение в истории диалога."""

    role: str  # "system" | "user" | "assistant" | "tool"
    content: str = ""
    tool_call_id: str | None = None
    tool_calls: list[LLMToolCall] = field(default_factory=list)


@dataclass(frozen=True)
class LLMRequest:
    """Запрос к LLM."""

    model: str
    messages: list[LLMMessage]
    tools: list[dict] | None = None
    max_tokens: int = 4096


class LLMClient(ABC):
    """Порт: стриминговый вызов LLM."""

    @abstractmethod
    def stream(self, request: LLMRequest) -> Iterator[LLMDelta]: ...
