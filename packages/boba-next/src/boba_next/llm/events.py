"""События LLM-слоя: от отправки запроса до finish_reason."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import StrEnum
from typing import Literal, TypeAlias, assert_never

from boba_next.llm.models import RequestId

__all__ = [
    "BaseLLMEvent",
    "FinishReason",
    "LLMAnswerStarted",
    "LLMAnswerToken",
    "LLMEvent",
    "LLMEventName",
    "LLMGenerationDone",
    "LLMGenerationStarted",
    "LLMLifecycleMarker",
    "LLMRefusalToken",
    "LLMRequestSent",
    "LLMRequestStarted",
    "LLMRetryAttempt",
    "LLMStreamingDelta",
    "LLMThinkingStarted",
    "LLMThinkingToken",
    "LLMToolCallArgumentDelta",
    "LLMToolCallBegin",
]


class FinishReason(StrEnum):
    """Нормализованная причина завершения генерации LLM."""

    STOP = "stop"
    LENGTH = "length"
    TOOL_CALLS = "tool_calls"
    CONTENT_FILTER = "content_filter"

    @property
    def is_terminal(self) -> bool:
        match self:
            case FinishReason.TOOL_CALLS:
                return False
            case FinishReason.STOP | FinishReason.LENGTH | FinishReason.CONTENT_FILTER:
                return True
            case _:
                assert_never(self)


@dataclass(frozen=True)
class BaseLLMEvent(ABC):
    """Базовый класс для всех событий LLM-слоя."""

    request_id: RequestId

    @classmethod
    @abstractmethod
    def name(cls) -> str: ...


@dataclass(frozen=True)
class LLMLifecycleMarker(BaseLLMEvent, ABC):
    """Граница фазы (старт/конец стадии, без контента)."""


@dataclass(frozen=True)
class LLMStreamingDelta(BaseLLMEvent, ABC):
    """Инкрементальный кусок контента между *Started и *Done."""


@dataclass(frozen=True)
class LLMRequestStarted(LLMLifecycleMarker):
    """HTTP-запрос к провайдеру вот-вот будет отправлен; парный к LLMRequestSent."""

    model: str
    messages_count: int
    has_tools: bool
    monotonic_ns: int

    @classmethod
    def name(cls) -> Literal["LLMRequestStarted"]:
        return "LLMRequestStarted"


@dataclass(frozen=True)
class LLMRequestSent(LLMLifecycleMarker):
    """HTTP-запрос отправлен, stream-handle получен; парный к LLMRequestStarted."""

    monotonic_ns: int

    @classmethod
    def name(cls) -> Literal["LLMRequestSent"]:
        return "LLMRequestSent"


@dataclass(frozen=True)
class LLMGenerationStarted(LLMLifecycleMarker):
    """Первый chunk получен."""

    @classmethod
    def name(cls) -> Literal["LLMGenerationStarted"]:
        return "LLMGenerationStarted"


@dataclass(frozen=True)
class LLMThinkingStarted(LLMLifecycleMarker):
    """Модель начала reasoning."""

    @classmethod
    def name(cls) -> Literal["LLMThinkingStarted"]:
        return "LLMThinkingStarted"


@dataclass(frozen=True)
class LLMThinkingToken(LLMStreamingDelta):
    """Chunk reasoning-токена."""

    token: str

    @classmethod
    def name(cls) -> Literal["LLMThinkingToken"]:
        return "LLMThinkingToken"


@dataclass(frozen=True)
class LLMAnswerStarted(LLMLifecycleMarker):
    """Модель начала отдавать ответ."""

    @classmethod
    def name(cls) -> Literal["LLMAnswerStarted"]:
        return "LLMAnswerStarted"


@dataclass(frozen=True)
class LLMAnswerToken(LLMStreamingDelta):
    """Chunk текстового ответа."""

    token: str

    @classmethod
    def name(cls) -> Literal["LLMAnswerToken"]:
        return "LLMAnswerToken"


@dataclass(frozen=True)
class LLMRefusalToken(LLMStreamingDelta):
    """Chunk отказа модели."""

    token: str

    @classmethod
    def name(cls) -> Literal["LLMRefusalToken"]:
        return "LLMRefusalToken"


@dataclass(frozen=True)
class LLMToolCallBegin(LLMLifecycleMarker):
    """Начало tool call — id и имя функции пришли."""

    index: int
    tool_call_id: str
    tool_name: str

    @classmethod
    def name(cls) -> Literal["LLMToolCallBegin"]:
        return "LLMToolCallBegin"


@dataclass(frozen=True)
class LLMToolCallArgumentDelta(LLMStreamingDelta):
    """Chunk аргументов tool call."""

    index: int
    arguments: str

    @classmethod
    def name(cls) -> Literal["LLMToolCallArgumentDelta"]:
        return "LLMToolCallArgumentDelta"


@dataclass(frozen=True)
class LLMRetryAttempt(LLMLifecycleMarker):
    """Попытка запроса к LLM будет повторена."""

    attempt: int
    reason: str
    status_code: int | None = None

    @classmethod
    def name(cls) -> Literal["LLMRetryAttempt"]:
        return "LLMRetryAttempt"


@dataclass(frozen=True)
class LLMGenerationDone(LLMLifecycleMarker):
    """Генерация завершена — пришёл finish_reason."""

    finish_reason: FinishReason = FinishReason.STOP

    def __post_init__(self) -> None:
        if not isinstance(self.finish_reason, FinishReason):
            object.__setattr__(self, "finish_reason", FinishReason(self.finish_reason))

    @classmethod
    def name(cls) -> Literal["LLMGenerationDone"]:
        return "LLMGenerationDone"


LLMEvent = (
    LLMRequestStarted
    | LLMRequestSent
    | LLMGenerationStarted
    | LLMThinkingStarted
    | LLMThinkingToken
    | LLMAnswerStarted
    | LLMAnswerToken
    | LLMRefusalToken
    | LLMToolCallBegin
    | LLMToolCallArgumentDelta
    | LLMRetryAttempt
    | LLMGenerationDone
)


LLMEventName: TypeAlias = Literal[
    "LLMRequestStarted",
    "LLMRequestSent",
    "LLMGenerationStarted",
    "LLMThinkingStarted",
    "LLMThinkingToken",
    "LLMAnswerStarted",
    "LLMAnswerToken",
    "LLMRefusalToken",
    "LLMToolCallBegin",
    "LLMToolCallArgumentDelta",
    "LLMRetryAttempt",
    "LLMGenerationDone",
]


def _verify_llm_event_names_exhaustive(e: LLMEvent) -> LLMEventName:
    """Exhaustiveness check для match-case над LLMEvent."""
    return e.name()
