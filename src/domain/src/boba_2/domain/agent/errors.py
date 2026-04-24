"""Агент-специфичные ошибки, построенные на маркерах core.errors.

Каждая concrete-ошибка явно объявляет свои эффекты миксами маркеров:

- :class:`~boba.domain.core.errors.UserFeedbackError` — event для sink;
- :class:`~boba.domain.core.errors.LLMFeedbackError` — фидбек в
  :class:`MessageService` для LLM;
- :class:`~boba.domain.core.errors.TerminalError` — остановка цикла;
- :class:`~boba.domain.core.errors.Retryable` — повтор в retry-middleware.

Маркеры ортогональны — конкретная ошибка выбирает любую комбинацию.
Generics привязываются прямо в объявлении класса:
``UserFeedbackError[RequestId, AgentEvent]``,
``LLMFeedbackError[LLMMessage]``.

Политика:

- Программные баги (``KeyError``, ``TypeError``, ``ValueError``,
  ``AssertionError``) — **не** наследуют :class:`RoutableError` и
  летят мимо роутера, крашат процесс.
- LLM-ошибки из :mod:`boba_2.domain.llm.errors` — изолированы и
  **не** :class:`RoutableError`. Мост делает
  :class:`~boba_2.domain.agent.meat.llm.LLMInvokeMiddleware`.
"""

from __future__ import annotations

from boba.domain.core.errors import (
    LLMFeedbackError,
    TerminalError,
    UserFeedbackError,
)
from boba_2.domain.agent.events import (
    AgentEvent,
    MaxIterationsReached,
    RepeatedFormatFailure,
    ToolCallFormatFailed,
    ToolExecutionFailed,
)
from boba_2.domain.llm.models import LLMMessage, RequestId


class MaxIterationsExceededError(
    UserFeedbackError[RequestId, AgentEvent], TerminalError
):
    """Цикл агента исчерпал лимит итераций без финального ответа.

    Поднимается :class:`~boba_2.domain.agent.meat.loop_control.\
IterationCounterMiddleware`.
    """

    def __init__(self, message: str, *, limit: int, iteration: int) -> None:
        super().__init__(message)
        self.limit = limit
        self.iteration = iteration

    def to_event(self, request_id: RequestId) -> AgentEvent:
        return MaxIterationsReached(
            request_id=request_id,
            error_kind=type(self).__name__,
            message=str(self),
            limit=self.limit,
            iteration=self.iteration,
        )


class RepeatedFormatFailureError(
    UserFeedbackError[RequestId, AgentEvent], TerminalError
):
    """Модель N раз подряд вывела неверный формат tool call.

    Поднимается :class:`~boba_2.domain.agent.meat.tools.\
RepeatedFormatFailureGuardMiddleware` после накопления ``limit``
    подряд :class:`~boba_2.domain.agent.events.ToolCallFormatFailed`
    без успешного :class:`~boba_2.domain.agent.events.ToolResultReady`
    между ними.
    """

    def __init__(self, message: str, *, count: int, limit: int) -> None:
        super().__init__(message)
        self.count = count
        self.limit = limit

    def to_event(self, request_id: RequestId) -> AgentEvent:
        return RepeatedFormatFailure(
            request_id=request_id,
            error_kind=type(self).__name__,
            message=str(self),
            count=self.count,
            limit=self.limit,
        )


class ToolFeedbackError(
    UserFeedbackError[RequestId, AgentEvent], LLMFeedbackError[LLMMessage]
):
    """Tool упал: sink видит событие, LLM видит фидбек в истории.

    Роутер:

    1. Пишет ``LLMMessage(role="tool", tool_call_id=<id>,
       content=<message>)`` в :class:`MessageService` — LLM на
       следующей итерации увидит объяснение ошибки и должна
       скорректировать поведение.
    2. Эмитит :class:`~boba_2.domain.agent.events.ToolExecutionFailed`
       в поток событий.

    Поднимается :class:`~boba_2.domain.agent.meat.tools.\
ToolExecutionMiddleware` — обогащает сырую
    :class:`~boba.domain.core.tools.ToolExecutionError` идентификатором
    tool call'а.
    """

    def __init__(
        self,
        *,
        tool_call_id: str,
        tool_name: str,
        error_kind: str,
        message: str,
    ) -> None:
        super().__init__(message)
        self.tool_call_id = tool_call_id
        self.tool_name = tool_name
        self.error_kind = error_kind
        self.message = message

    def to_event(self, request_id: RequestId) -> AgentEvent:
        return ToolExecutionFailed(
            request_id=request_id,
            tool_call_id=self.tool_call_id,
            tool_name=self.tool_name,
            error_kind=self.error_kind,
            message=self.message,
        )

    def to_llm_feedback(self) -> LLMMessage:
        return LLMMessage(
            role="tool",
            content=self.message,
            tool_call_id=self.tool_call_id,
        )


class LLMToolCallFormatError(
    UserFeedbackError[RequestId, AgentEvent], LLMFeedbackError[LLMMessage]
):
    """LLM нарушила формат content-as-JSON tool call'а.

    Роутер:

    1. Пишет ``LLMMessage(role="user", content=<критика>)`` в
       :class:`MessageService` — на следующей итерации модель увидит
       feedback как user-сообщение.
    2. Эмитит :class:`~boba_2.domain.agent.events.ToolCallFormatFailed`.

    Поднимается парсером content-as-JSON
    (:class:`~boba_2.domain.agent.meat.content_tool_call.strict.\
StrictJsonToolCallParser`), когда LLM эмитит JSON-объект с
    неверной структурой.
    """

    def __init__(self, message: str, *, raw_content: str) -> None:
        super().__init__(message)
        self.raw_content = raw_content

    def to_event(self, request_id: RequestId) -> AgentEvent:
        return ToolCallFormatFailed(
            request_id=request_id,
            error_kind=type(self).__name__,
            message=str(self),
        )

    def to_llm_feedback(self) -> LLMMessage:
        return LLMMessage(role="user", content=str(self))
