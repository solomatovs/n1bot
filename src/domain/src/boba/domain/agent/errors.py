"""Агент-специфичные ошибки, построенные на маркерах core.errors.

Каждая concrete-ошибка явно объявляет свои эффекты миксами маркеров:

- :class:`~boba.domain.core.errors.UserFeedbackError` — event для sink;
- :class:`~boba.domain.core.errors.LLMFeedbackError` — фидбек в
  :class:`MessageService` для LLM;
- :class:`~boba.domain.core.errors.TerminalError` — остановка цикла;

Маркеры ортогональны — конкретная ошибка выбирает любую комбинацию.
Generics привязываются прямо в объявлении класса:
``UserFeedbackError[RequestId, AgentEvent]``,
``LLMFeedbackError[LLMMessage]``.

Политика:

- Программные баги (``KeyError``, ``TypeError``, ``ValueError``,
  ``AssertionError``) — **не** наследуют :class:`RoutableError` и
  летят мимо роутера, крашат процесс.
- LLM-ошибки из :mod:`boba.domain.llm.errors` — изолированы и
  **не** :class:`RoutableError`. Мост делает
  :class:`~boba.domain.agent.meat.llm.LLMInvokeMiddleware`.
"""

from __future__ import annotations

from boba.domain.agent.events import (
    AgentEvent,
    GenerationFailed,
    MaxIterationsReached,
    RepeatedFormatFailure,
    ToolCallFormatFailed,
)
from boba.domain.core.errors import (
    LLMFeedbackError,
    TerminalError,
    UserFeedbackError,
)
from boba.domain.llm.models import LLMMessage, RequestId


class MaxIterationsExceededError(TerminalError[RequestId, AgentEvent]):
    """Цикл агента исчерпал лимит итераций без финального ответа.

    Поднимается :class:`~boba.domain.agent.meat.loop_control.\
IterationCounterMiddleware`.
    """

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


class RepeatedFormatFailureError(TerminalError[RequestId, AgentEvent]):
    """Модель N раз подряд вывела неверный формат tool call.

    Поднимается :class:`~boba.domain.agent.meat.tools.\
RepeatedFormatFailureGuardMiddleware` после накопления ``limit``
    подряд :class:`~boba.domain.agent.events.ToolCallFormatFailed`
    без успешного :class:`~boba.domain.agent.events.ToolResultReady`
    между ними.
    """

    def __init__(self, message: str, *, count: int, limit: int) -> None:
        super().__init__(message)
        self.count = count
        self.limit = limit

    def to_user_feedback(self, request_id: RequestId) -> RepeatedFormatFailure:
        return RepeatedFormatFailure(
            request_id=request_id,
            error_kind=type(self).__name__,
            message=str(self),
            count=self.count,
            limit=self.limit,
        )


class LLMGenerationFailedError(TerminalError[RequestId, AgentEvent]):
    """Мост: исключение LLM-слоя, дошедшее до границы агента.

    :mod:`boba.domain.llm.errors` — изолированная иерархия, её типы
    **не** :class:`RoutableError`. Но агент-слой всё равно должен
    пропустить отказ через :class:`AgentErrorRouter`, чтобы цикл
    остановился единообразно с остальными терминальными ошибками.

    :class:`~boba.domain.agent.meat.llm.LLMInvokeMiddleware` ловит
    :class:`~boba.domain.llm.errors.LLMError`, снимает с него
    ``error_kind``-маркер и поднимает эту обёртку —
    роутер маршрутизирует её стандартно и эмитит :class:`GenerationFailed`
    через :meth:`to_user_feedback`.
    """

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


class LLMToolCallFormatError(
    UserFeedbackError[RequestId, AgentEvent], LLMFeedbackError[LLMMessage]
):
    """LLM нарушила формат content-as-JSON tool call'а.

    Роутер:

    1. Пишет ``LLMMessage(role="user", content=<критика>)`` в
       :class:`MessageService` — на следующей итерации модель увидит
       feedback как user-сообщение.
    2. Эмитит :class:`~boba.domain.agent.events.ToolCallFormatFailed`.

    Поднимается парсером content-as-JSON
    (:class:`~boba.domain.agent.meat.content_tool_call.strict.\
StrictJsonToolCallParser`), когда LLM эмитит JSON-объект с
    неверной структурой.
    """

    def __init__(self, message: str, *, raw_content: str) -> None:
        super().__init__(message)
        self.raw_content = raw_content

    def to_user_feedback(self, request_id: RequestId) -> AgentEvent:
        return ToolCallFormatFailed(
            request_id=request_id,
            error_kind=type(self).__name__,
            message=str(self),
        )

    def to_llm_feedback(self) -> LLMMessage:
        return LLMMessage(role="user", content=str(self))
