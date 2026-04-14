from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterator


# ── LLM models ──


@dataclass(frozen=True)
class LLMToolCall:
    """Готовый tool call (для LLMMessage)."""

    id: str
    name: str
    arguments: str


@dataclass(frozen=True)
class LLMMessage:
    """Одно сообщение в истории диалога."""

    role: str
    content: str
    tool_call_id: str | None = None
    tool_calls: list[LLMToolCall] = field(default_factory=list)


@dataclass(frozen=True)
class LLMRequest:
    """Запрос к LLM."""

    model: str
    messages: Iterator[LLMMessage]


# ── Agent models ──


@dataclass(frozen=True)
class AgentRequest:
    """Входные данные для AgentLoop."""

    query: str
    model: str
    max_tokens: int = 4096


@dataclass(frozen=True)
class AgentConfig:
    """Настройки AgentLoop."""

    max_iterations: int = 20
    limit_message: str = "Достигнут лимит итераций агента."


@dataclass
class AgentContext:
    """
    Мутабельный контекст, передаваемый через стадии Pipeline.
    Стадии читают и дополняют его на каждой итерации цикла.

    Messages живут в MessageService, tool_calls — промежуточный результат GenerateStage.
    """

    request: AgentRequest
    config: AgentConfig
    iteration: int = 0
