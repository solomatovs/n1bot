"""Маркеры доменных ошибок для полиморфной маршрутизации."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Generic, TypeVar

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
