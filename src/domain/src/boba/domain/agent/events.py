from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from boba.domain.agent.models import RequestId
from boba.domain.core.patterns import Converter, Serializer


@dataclass(frozen=True)
class BaseEvent(ABC):
    """Базовый класс для всех событий агента."""

    request_id: RequestId

    @classmethod
    @abstractmethod
    def name(cls) -> str:
        """Стабильное имя типа события для сериализации.

        Каждый подкласс возвращает свою строку. Отвязывает wire-формат от
        имени класса: переименование класса не ломает существующие логи.
        """
        ...


@dataclass(frozen=True)
class UserQueryReceived(BaseEvent):
    """Запрос пользователя принят."""

    query: str

    @classmethod
    def name(cls) -> str:
        return "UserQueryReceived"


@dataclass(frozen=True)
class StageStarted(BaseEvent):
    stage: str

    @classmethod
    def name(cls) -> str:
        return "StageStarted"


@dataclass(frozen=True)
class StageCompleted(BaseEvent):
    stage: str
    detail: str

    @classmethod
    def name(cls) -> str:
        return "StageCompleted"


@dataclass(frozen=True)
class GenerationStarted(BaseEvent):
    """Первый chunk от LLM — генерация началась."""

    @classmethod
    def name(cls) -> str:
        return "GenerationStarted"


@dataclass(frozen=True)
class ThinkingStarted(BaseEvent):
    """Модель начала thinking/reasoning."""

    @classmethod
    def name(cls) -> str:
        return "ThinkingStarted"


@dataclass(frozen=True)
class ThinkingToken(BaseEvent):
    """Chunk thinking/reasoning от LLM."""

    token: str

    @classmethod
    def name(cls) -> str:
        return "ThinkingToken"


@dataclass(frozen=True)
class AnswerStarted(BaseEvent):
    """Модель начала генерировать ответ."""

    @classmethod
    def name(cls) -> str:
        return "AnswerStarted"


@dataclass(frozen=True)
class AnswerToken(BaseEvent):
    """Chunk текстового ответа от LLM."""

    token: str

    @classmethod
    def name(cls) -> str:
        return "AnswerToken"


@dataclass(frozen=True)
class RefusalToken(BaseEvent):
    """Chunk отказа модели отвечать."""

    token: str

    @classmethod
    def name(cls) -> str:
        return "RefusalToken"


@dataclass(frozen=True)
class GenerationDone(BaseEvent):
    """Генерация завершена."""

    finish_reason: str = "stop"  # "stop", "tool_calls", "length"

    @classmethod
    def name(cls) -> str:
        return "GenerationDone"


@dataclass(frozen=True)
class ToolCallBegin(BaseEvent):
    """Начало tool call — пришёл id и имя функции."""

    index: int
    tool_call_id: str
    tool_name: str

    @classmethod
    def name(cls) -> str:
        return "ToolCallBegin"


@dataclass(frozen=True)
class ToolCallArgumentDelta(BaseEvent):
    """Chunk аргументов tool call."""

    index: int
    arguments: str

    @classmethod
    def name(cls) -> str:
        return "ToolCallArgumentDelta"


@dataclass(frozen=True)
class ToolCallComplete(BaseEvent):
    """Агрегированный tool call: имя + полные аргументы."""

    tool_call_id: str
    tool_name: str
    arguments: str

    @classmethod
    def name(cls) -> str:
        return "ToolCallComplete"


@dataclass(frozen=True)
class ToolResultReady(BaseEvent):
    tool_call_id: str
    tool_name: str
    content: str
    is_error: bool = False

    @classmethod
    def name(cls) -> str:
        return "ToolResultReady"


@dataclass(frozen=True)
class ThinkingComplete(BaseEvent):
    """Агрегированный thinking: весь текст рассуждений."""

    content: str

    @classmethod
    def name(cls) -> str:
        return "ThinkingComplete"


@dataclass(frozen=True)
class AnswerComplete(BaseEvent):
    """Агрегированный ответ: весь текст ответа."""

    content: str

    @classmethod
    def name(cls) -> str:
        return "AnswerComplete"


@dataclass(frozen=True)
class RefusalComplete(BaseEvent):
    """Агрегированный отказ: весь текст отказа."""

    content: str

    @classmethod
    def name(cls) -> str:
        return "RefusalComplete"


AgentEvent = (
    UserQueryReceived
    | StageStarted
    | StageCompleted
    | GenerationStarted
    | ThinkingStarted
    | ThinkingToken
    | ThinkingComplete
    | AnswerStarted
    | AnswerToken
    | AnswerComplete
    | RefusalToken
    | RefusalComplete
    | GenerationDone
    | ToolCallBegin
    | ToolCallArgumentDelta
    | ToolCallComplete
    | ToolResultReady
)


# Типы сериализации AgentEvent
EventEncoder = Converter[AgentEvent, str]
EventDecoder = Converter[str, AgentEvent]
EventSerializer = Serializer[AgentEvent, str]
