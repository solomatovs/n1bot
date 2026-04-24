"""Ошибки агент-слоя с routing-таксономией.

Иерархия выстроена на базовых generics из
:mod:`boba.domain.core.errors`: binding-бейзы привязывают generics к
агентским типам (``RequestId`` / ``AgentEvent`` / ``LLMMessage``), от
них наследуются concrete-ошибки и реализуют ``to_*_event`` методы.
:class:`~boba_2.domain.agent.meat.error_routing.AgentErrorRouter`
полиморфно маршрутизирует, не зная о подклассах.

Политика:

- **Программные баги** (``KeyError``, ``TypeError``, ``ValueError``,
  ``AssertionError``) — **не** наследуют :class:`RoutableError` и
  летят мимо роутера, крашат процесс. Маскировать их под доменные
  — запрещено.
- :class:`LLMError` из LLM-слоя (:mod:`boba_2.domain.llm.errors`) —
  изолирована, **не** :class:`RoutableError`.
  :class:`~boba_2.domain.agent.meat.llm_invoke.LLMInvokeMiddleware`
  сам перехватывает её и эмитит :class:`GenerationFailed` — мост
  между слоями ручной, но узкий и предсказуемый.

════════════════════════════════════════════════════════════════════
  Иерархия
════════════════════════════════════════════════════════════════════

::

    RoutableError                                  [core.errors]
    │
    ├── UserFeedbackError[TReqId, TUserEvent]      [core.errors]
    │   │   to_user_event(request_id) -> TUserEvent
    │   │
    │   ├── TerminalError                            + цикл стоп
    │   │   │
    │   │   └── AgentTerminalError                 [boba_2] binding
    │   │       │   bind: TReqId=RequestId, TUserEvent=AgentEvent
    │   │       │
    │   │       ├── MaxIterationsExceededError     → MaxIterationsReached
    │   │       └── RepeatedFormatFailureError     → RepeatedFormatFailure
    │   │
    │   ├── UserNoticeError                         нотис + severity
    │   │   │
    │   │   └── AgentUserNotice                    [boba_2] concrete
    │   │           → UserNoticeReady (TBD)
    │   │
    │   └── LLMFeedbackError[TReqId, TUserEvent,   [core.errors]
    │       │                TFeedback]              + LLM feedback
    │       │   to_llm_feedback() -> TFeedback       (цикл идёт)
    │       │
    │       └── AgentLLMFeedbackError              [boba_2] binding
    │           │   bind: + TFeedback=LLMMessage
    │           │
    │           ├── ToolFeedbackError              → ToolExecutionFailed
    │           │                                    + role="tool" msg
    │           └── LLMToolCallFormatError         → ToolCallFormatFailed
    │                                                + role="user" critique
    │
    └── Retryable                                   marker, ортогонально
"""

from __future__ import annotations

from abc import ABC

from boba.domain.core.errors import (
    LLMFeedbackError,
    TerminalError,
    UserNoticeError,
)
from boba_2.domain.agent.events import (
    AgentEvent,
    MaxIterationsReached,
    RepeatedFormatFailure,
    ToolCallFormatFailed,
    ToolExecutionFailed,
)
from boba_2.domain.llm.models import LLMMessage, RequestId

# ═════════════════════════════════════════════════════════════════════
#  Binding-бейзы: привязываем generics из core к agent-типам
# ═════════════════════════════════════════════════════════════════════


class AgentTerminalError(TerminalError[RequestId, AgentEvent], ABC):
    """Binding ``TerminalError[TReqId=RequestId, TUserEvent=AgentEvent]``.

    Concrete-терминальные ошибки агента наследуются отсюда и
    реализуют ``to_user_event``, возвращая конкретное
    ``*Failed``-событие (подкласс
    :class:`~boba_2.domain.agent.events.TerminalFailure`).
    :class:`StopOnAnyFailure` реагирует на любое ``TerminalFailure``
    и останавливает цикл.
    """


class AgentLLMFeedbackError(
    LLMFeedbackError[RequestId, AgentEvent, LLMMessage],
    ABC,
):
    """Binding ``LLMFeedbackError[RequestId, AgentEvent, LLMMessage]``.

    Concrete LLM-feedback ошибки наследуются отсюда и реализуют
    ``to_user_event`` (унаследован от :class:`UserFeedbackError`) +
    :meth:`to_llm_feedback` (возвращает :class:`LLMMessage` —
    обычно ``role="tool"`` или ``role="user"``).
    """


# ═════════════════════════════════════════════════════════════════════
#  Terminal errors (цикл останавливается)
# ═════════════════════════════════════════════════════════════════════


class MaxIterationsExceededError(AgentTerminalError):
    """Агентский цикл исчерпал лимит итераций без терминального ответа.

    Поднимается :class:`~boba_2.domain.agent.meat.loop_control.\
IterationCounterMiddleware`. Роутер конвертирует в
    :class:`MaxIterationsReached`, :class:`StopOnAnyFailure`
    останавливает цикл.
    """

    def __init__(self, message: str, *, limit: int, iteration: int) -> None:
        super().__init__(message)
        self.limit = limit
        self.iteration = iteration

    def to_user_event(self, request_id: RequestId) -> AgentEvent:
        return MaxIterationsReached(
            request_id=request_id,
            error_kind=type(self).__name__,
            message=str(self),
            limit=self.limit,
            iteration=self.iteration,
        )


class RepeatedFormatFailureError(AgentTerminalError):
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

    def to_user_event(self, request_id: RequestId) -> AgentEvent:
        return RepeatedFormatFailure(
            request_id=request_id,
            error_kind=type(self).__name__,
            message=str(self),
            count=self.count,
            limit=self.limit,
        )


# ═════════════════════════════════════════════════════════════════════
#  LLM feedback (цикл продолжается, LLM видит фидбек)
# ═════════════════════════════════════════════════════════════════════


class ToolFeedbackError(AgentLLMFeedbackError):
    """Ошибка выполнения конкретного tool'а.

    Роутер:

    1. Пишет ``LLMMessage(role="tool", tool_call_id=<id>,
       content=<message>)`` в :class:`MessageService` — LLM на
       следующей итерации увидит объяснение ошибки в истории и
       должна скорректировать поведение (повторить с другими args
       или ответить пользователю обычным текстом).
    2. Эмитит :class:`ToolExecutionFailed` в поток событий — sink
       UI/журнала видит факт падения.

    Поднимается :class:`~boba_2.domain.agent.meat.tools.\
ToolExecutionMiddleware`: обогащает сырую :class:`ToolExecutionError`
    (или ошибку парсинга JSON-args) идентификатором tool call'а.
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

    def to_user_event(self, request_id: RequestId) -> AgentEvent:
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


class LLMToolCallFormatError(AgentLLMFeedbackError):
    """LLM нарушила формат content-as-JSON tool call'а.

    Роутер:

    1. Пишет ``LLMMessage(role="user", content=<критика>)`` в
       :class:`MessageService` — на следующей итерации модель увидит
       feedback как user-сообщение и должна скорректировать вывод.
    2. Эмитит :class:`ToolCallFormatFailed` в поток.

    Поднимается парсером content-as-JSON
    (:class:`~boba_2.domain.agent.meat.content_tool_call.strict.\
StrictJsonToolCallParser`), когда LLM эмитит JSON-объект с
    неверной структурой (невалидный JSON, не-объект на корне,
    отсутствующие/посторонние поля, неверные типы).

    ``message`` уже включает цитату сырого content и описание, что
    именно не так — LLM получает самодостаточный фидбек.
    """

    def __init__(self, message: str, *, raw_content: str) -> None:
        super().__init__(message)
        self.message = message
        self.raw_content = raw_content

    def to_user_event(self, request_id: RequestId) -> AgentEvent:
        return ToolCallFormatFailed(
            request_id=request_id,
            error_kind=type(self).__name__,
            message=self.message,
        )

    def to_llm_feedback(self) -> LLMMessage:
        return LLMMessage(role="user", content=self.message)


# ═════════════════════════════════════════════════════════════════════
#  User notices (цикл продолжается, severity)
# ═════════════════════════════════════════════════════════════════════


class AgentUserNotice(UserNoticeError[RequestId, AgentEvent]):
    """Нотис пользователю с ``severity``.

    Цикл не прерывается — sink получает уведомление и отрисовывает.
    Конкретный ``to_user_event`` появится вместе с ``UserNoticeReady``
    событием при миграции соответствующих use-cases (deprecation-
    warnings, soft-reject валидации). На данном этапе — заглушка
    через ``raise NotImplementedError``: класс в иерархии
    присутствует, но без события не полиморфит.
    """

    def to_user_event(self, request_id: RequestId) -> AgentEvent:
        raise NotImplementedError(
            "AgentUserNotice.to_user_event появится с UserNoticeReady-событием",
        )
