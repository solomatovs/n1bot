"""Порт LLM-клиента. Domain не знает про litellm/openai — только этот контракт."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterator

from boba.domain.core.stream import StreamSource


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
    """
    Одно сообщение в истории диалога
    """

    role: str
    content: str
    tool_call_id: str | None = None
    tool_calls: list[LLMToolCall] = field(default_factory=list)


@dataclass(frozen=True)
class LLMRequest:
    """Запрос к LLM."""

    model: str
    messages: Iterator[LLMMessage]


class LLMCompletionService(StreamSource[LLMRequest, LLMDelta]):
    """
    Базовый класс для любого LLM-источника — адаптер или middleware.
    Адаптер реализует produce() напрямую.
    Middleware принимает next в __init__ и делегирует ему.
    """
