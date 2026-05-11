"""Маркеры routable-ошибок агента и агент-специализации на них."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Generic, TypeVar

from boba.agent.events import (
    AgentEvent,
    GenerationFailed,
    MaxIterationsReached,
)
from boba.agent.models import LLMFeedback
from boba.llm.models import RequestId

TReqId = TypeVar("TReqId")
TUserEvent = TypeVar("TUserEvent")
TFeedback = TypeVar("TFeedback")


class RoutableError(Exception):
    """Базовый класс всех маршрутизируемых доменных ошибок."""


class UserFeedbackError(RoutableError, Generic[TReqId, TUserEvent], ABC):
    """Маркер: ошибка превращается в событие для sink'ов."""

    @abstractmethod
    def to_user_feedback(self, request_id: TReqId) -> TUserEvent:
        """Построить observability-событие для stream."""
        ...


class LLMFeedbackError(RoutableError, Generic[TFeedback], ABC):
    """Маркер: ошибка добавляется в историю как сообщение для LLM."""

    @abstractmethod
    def to_llm_feedback(self) -> TFeedback:
        """Сообщение, которое роутер добавит в историю."""
        ...


class TerminalError(UserFeedbackError[TReqId, TUserEvent], ABC):
    """Маркер: ошибка останавливает агентский цикл."""


class AgentLLMFeedbackError(LLMFeedbackError[LLMFeedback], ABC):
    """Agent-уровневая специализация LLMFeedbackError с TFeedback=LLMFeedback."""


class MaxIterationsExceededError(TerminalError[RequestId, AgentEvent]):
    """Цикл агента исчерпал лимит итераций без финального ответа."""

    def __init__(self, message: str, *, limit: int, iteration: int) -> None:
        super().__init__(message)
        self.limit = limit
        self.iteration = iteration

    def to_user_feedback(self, request_id: RequestId) -> MaxIterationsReached:
        return MaxIterationsReached(
            request_id=request_id,
            error_kind=type(self).__name__,
            message=str(self),
            limit=self.limit,
            iteration=self.iteration,
        )


class LLMGenerationFailedError(TerminalError[RequestId, AgentEvent]):
    """Обёртка над LLMError для маршрутизации через AgentErrorRouter."""

    def __init__(
        self,
        message: str,
        *,
        error_kind: str,
    ) -> None:
        super().__init__(message)
        self.error_kind = error_kind

    def to_user_feedback(self, request_id: RequestId) -> GenerationFailed:
        return GenerationFailed(
            request_id=request_id,
            error_kind=self.error_kind,
            message=str(self),
        )
