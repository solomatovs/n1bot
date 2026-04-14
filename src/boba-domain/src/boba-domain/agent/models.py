from __future__ import annotations

from dataclasses import dataclass
from typing import Union


@dataclass(frozen=True)
class StageStarted:
    stage: str


@dataclass(frozen=True)
class StageCompleted:
    stage: str
    detail: str


# Генерация (стриминг)
@dataclass(frozen=True)
class ThinkingToken:
    token: str


@dataclass(frozen=True)
class AnswerToken:
    token: str


@dataclass(frozen=True)
class GenerationDone:
    pass


@dataclass(frozen=True)
class ToolCallStarted:
    tool_call_id: str
    tool_name: str
    arguments: str


@dataclass(frozen=True)
class ToolResultReady:
    tool_call_id: str
    tool_name: str
    content: str
    is_error: bool = False


AgentEvent = Union[
    StageStarted,
    StageCompleted,
    ThinkingToken,
    AnswerToken,
    GenerationDone,
    ToolCallStarted,
    ToolResultReady,
]


@dataclass(frozen=True)
class AgentRequest:
    """Входные данные для AgentLoop.run()."""

    query: str
    model: str
    max_tokens: int


@dataclass(frozen=True)
class AgentConfig:
    """Настройки AgentLoop."""

    max_iterations: int
    default_model: str
    limit_message: str


@dataclass
class AgentContext:
    """
    Контекст, передаваемый через Pipeline и AgentLoop.
    Мутабельный — стадии и цикл дополняют его.
    """

    request: AgentRequest
    config: AgentConfig
