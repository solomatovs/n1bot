from __future__ import annotations

from dataclasses import dataclass, field

from boba.domain.agent.llm import LLMMessage, LLMToolCall


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
    default_model: str = "gpt-4o"
    limit_message: str = "Достигнут лимит итераций агента."


@dataclass
class AgentContext:
    """
    Мутабельный контекст, передаваемый через стадии Pipeline.
    Стадии читают и дополняют его на каждой итерации цикла.
    """

    request: AgentRequest
    config: AgentConfig
    messages: list[LLMMessage] = field(default_factory=list)
    pending_tool_calls: list[LLMToolCall] = field(default_factory=list)
    iteration: int = 0
