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

__all__ = [
    "AgentLLMFeedbackError",
    "LLMFeedbackError",
    "LLMGenerationFailedError",
    "MaxIterationsExceededError",
    "RoutableError",
    "TerminalError",
    "UserFeedbackError",
    "extract_error_context",
]

TReqId = TypeVar("TReqId")
TUserEvent = TypeVar("TUserEvent")
TFeedback = TypeVar("TFeedback")


def extract_error_context(
    exc: BaseException,
) -> tuple[int | None, tuple[str, ...]]:
    """Свести исключение к (status_code, cause_chain) для error-событий.

    `status_code` — первый int-атрибут с этим именем во всей цепочке (сам exc
    + его `__cause__`/`__context__`).
    `cause_chain` — список «Type: message» начиная с `exc.__cause__` (или
    `__context__`) и глубже. Сам `exc` в цепочку не попадает — его текст
    уже несёт обёртка `RoutableError` (str(self)). Сообщения, дословно
    повторяющие уже виденное, пропускаются — типична ситуация, когда
    `RoutableError(str(e)) from e` создаёт ровно ту же строку, что у `e`.
    """
    items: list[str] = []
    sc: int | None = _status_code_of(exc)
    seen_ids: set[int] = {id(exc)}
    seen_messages: set[str] = {str(exc)}
    current: BaseException | None = exc.__cause__ or exc.__context__
    while current is not None and id(current) not in seen_ids:
        seen_ids.add(id(current))
        message = str(current)
        if message not in seen_messages:
            items.append(f"{type(current).__name__}: {message}")
            seen_messages.add(message)
        if sc is None:
            sc = _status_code_of(current)
        current = current.__cause__ or current.__context__
    return sc, tuple(items)


def _status_code_of(exc: BaseException) -> int | None:
    sc = getattr(exc, "status_code", None)
    return sc if isinstance(sc, int) else None


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
            iteration_count=self.iteration,
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
