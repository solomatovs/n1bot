from __future__ import annotations

from dataclasses import dataclass


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
