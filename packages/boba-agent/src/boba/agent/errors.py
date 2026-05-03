"""Агент-специфичные ошибки на маркерах core.errors."""

from __future__ import annotations

from abc import ABC

from boba.agent.events import (
    AgentEvent,
    GenerationFailed,
    MaxIterationsReached,
)
from boba.agent.payloads import LLMFeedback
from boba.errors import LLMFeedbackError, TerminalError
from boba.llm.models import RequestId


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
