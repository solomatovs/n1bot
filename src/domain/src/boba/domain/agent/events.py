from __future__ import annotations

from dataclasses import dataclass
from typing import Union
from boba.domain.agent.models import RequestId


@dataclass(frozen=True)
class BaseEvent:
    """Базовый класс для всех событий агента."""
    request_id: RequestId


@dataclass(frozen=True)
class UserQueryReceived(BaseEvent):
    """Запрос пользователя принят."""
    query: str


@dataclass(frozen=True)
class StageStarted(BaseEvent):
    stage: str


@dataclass(frozen=True)
class StageCompleted(BaseEvent):
    stage: str
    detail: str


@dataclass(frozen=True)
class GenerationStarted(BaseEvent):
    """Первый chunk от LLM — генерация началась."""


@dataclass(frozen=True)
class ThinkingStarted(BaseEvent):
    """Модель начала thinking/reasoning."""


@dataclass(frozen=True)
class ThinkingToken(BaseEvent):
    """Chunk thinking/reasoning от LLM."""

    token: str


@dataclass(frozen=True)
class AnswerStarted(BaseEvent):
    """Модель начала генерировать ответ."""


@dataclass(frozen=True)
class AnswerToken(BaseEvent):
    """Chunk текстового ответа от LLM."""

    token: str


@dataclass(frozen=True)
class RefusalToken(BaseEvent):
    """Chunk отказа модели отвечать."""

    token: str


@dataclass(frozen=True)
class GenerationDone(BaseEvent):
    """Генерация завершена."""

    finish_reason: str = "stop"  # "stop", "tool_calls", "length"


@dataclass(frozen=True)
class ToolCallBegin(BaseEvent):
    """Начало tool call — пришёл id и имя функции."""

    index: int
    tool_call_id: str
    tool_name: str


@dataclass(frozen=True)
class ToolCallArgumentDelta(BaseEvent):
    """Chunk аргументов tool call."""

    index: int
    arguments: str


@dataclass(frozen=True)
class ToolCallComplete(BaseEvent):
    """Агрегированный tool call: имя + полные аргументы."""

    tool_call_id: str
    tool_name: str
    arguments: str


@dataclass(frozen=True)
class ToolResultReady(BaseEvent):
    tool_call_id: str
    tool_name: str
    content: str
    is_error: bool = False


@dataclass(frozen=True)
class ThinkingComplete(BaseEvent):
    """Агрегированный thinking: весь текст рассуждений."""

    content: str


@dataclass(frozen=True)
class AnswerComplete(BaseEvent):
    """Агрегированный ответ: весь текст ответа."""

    content: str


@dataclass(frozen=True)
class RefusalComplete(BaseEvent):
    """Агрегированный отказ: весь текст отказа."""

    content: str


AgentEvent = Union[
    UserQueryReceived,
    StageStarted,
    StageCompleted,
    GenerationStarted,
    ThinkingStarted,
    ThinkingToken,
    ThinkingComplete,
    AnswerStarted,
    AnswerToken,
    AnswerComplete,
    RefusalToken,
    RefusalComplete,
    GenerationDone,
    ToolCallBegin,
    ToolCallArgumentDelta,
    ToolCallComplete,
    ToolResultReady,
]
