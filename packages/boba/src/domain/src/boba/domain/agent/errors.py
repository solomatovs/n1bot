"""Агент-специфичные ошибки, построенные на маркерах core.errors.

Каждая concrete-ошибка явно объявляет свои эффекты миксами маркеров:

- :class:`~boba.domain.core.errors.UserFeedbackError` — event для sink;
- :class:`~boba.domain.core.errors.LLMFeedbackError` — фидбек в
  :class:`MessageService` для LLM;
- :class:`~boba.domain.core.errors.TerminalError` — остановка цикла;

Маркеры ортогональны — конкретная ошибка выбирает любую комбинацию.

Политика:

- Программные баги (``KeyError``, ``TypeError``, ``ValueError``,
  ``AssertionError``) — **не** наследуют :class:`RoutableError` и
  летят мимо роутера, крашат процесс.
- LLM-ошибки из :mod:`boba.domain.llm.errors` — изолированы и
  **не** :class:`RoutableError`. Мост делает
  :class:`~boba.domain.agent.middleware.llm.LLMInvokeMiddleware`.
"""

from __future__ import annotations

from abc import ABC

from boba.domain.agent.events import (
    AgentEvent,
    GenerationFailed,
    MaxIterationsReached,
)
from boba.domain.agent.payloads import LLMFeedback
from boba.domain.core.errors import LLMFeedbackError, TerminalError
from boba.domain.llm.models import RequestId


class AgentLLMFeedbackError(LLMFeedbackError[LLMFeedback], ABC):
    """Agent-уровневая специализация :class:`LLMFeedbackError`.

    Фиксирует ``TFeedback = LLMFeedback`` — discriminated union из
    :class:`LLMCritique` / :class:`ToolCallRejection`. Это даёт
    типизированное narrowing в :class:`AgentErrorRouter` (match-case
    раскрывает union в конкретные варианты) и не пропускает «снаружи»
    raw ``LLMMessage`` через writer: каждый вариант мапится в свой
    узкий метод :class:`DialogueWriter`.

    Concrete-ошибки агента, которые хотят писать feedback в историю
    через роутер, наследуются **от этого класса**, а не от
    :class:`LLMFeedbackError` напрямую, и реализуют ``to_llm_feedback``
    с возвратом одного из вариантов :class:`LLMFeedback`.
    """


class MaxIterationsExceededError(TerminalError[RequestId, AgentEvent]):
    """Цикл агента исчерпал лимит итераций без финального ответа.

    Поднимается :class:`~boba.domain.agent.middleware.loop_control.\
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


class LLMGenerationFailedError(TerminalError[RequestId, AgentEvent]):
    """Мост: исключение LLM-слоя, дошедшее до границы агента.

    :mod:`boba.domain.llm.errors` — изолированная иерархия, её типы
    **не** :class:`RoutableError`. Но агент-слой всё равно должен
    пропустить отказ через :class:`AgentErrorRouter`, чтобы цикл
    остановился единообразно с остальными терминальными ошибками.

    :class:`~boba.domain.agent.middleware.llm.LLMInvokeMiddleware` ловит
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
