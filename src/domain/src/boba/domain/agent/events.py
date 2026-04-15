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


@dataclass(frozen=True)
class GenerationStarted:
    """Первый chunk от LLM — генерация началась."""


@dataclass(frozen=True)
class ThinkingStarted:
    """Модель начала thinking/reasoning."""


@dataclass(frozen=True)
class ThinkingToken:
    """Chunk thinking/reasoning от LLM."""

    token: str


@dataclass(frozen=True)
class AnswerStarted:
    """Модель начала генерировать ответ."""


@dataclass(frozen=True)
class AnswerToken:
    """Chunk текстового ответа от LLM."""

    token: str


@dataclass(frozen=True)
class RefusalToken:
    """Chunk отказа модели отвечать."""

    token: str


@dataclass(frozen=True)
class GenerationDone:
    """Генерация завершена."""

    finish_reason: str = "stop"  # "stop", "tool_calls", "length"


@dataclass(frozen=True)
class ToolCallBegin:
    """Начало tool call — пришёл id и имя функции."""

    index: int
    tool_call_id: str
    tool_name: str


@dataclass(frozen=True)
class ToolCallArgumentDelta:
    """Chunk аргументов tool call."""

    index: int
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
    GenerationStarted,
    ThinkingStarted,
    ThinkingToken,
    AnswerStarted,
    AnswerToken,
    RefusalToken,
    GenerationDone,
    ToolCallBegin,
    ToolCallArgumentDelta,
    ToolResultReady,
]
