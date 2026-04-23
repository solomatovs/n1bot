"""Базовая routing-таксономия доменных ошибок.

Живёт в ``core``, потому что используется agent-слоем
(:mod:`boba.domain.agent.errors`, :mod:`boba.domain.agent.messages`,
:mod:`boba.domain.agent.prompt`). Конкретные агентские подклассы (LLM-иерархия,
:class:`~boba.domain.agent.errors.ToolFeedbackError`) — в
:mod:`boba.domain.agent.errors`.

Первичная ось — **эффект при обработке**: user видит event через
sink; LLM опционально получает feedback через ``MessageService``;
цикл либо продолжается, либо останавливается.

**Важно:** events — канал *только* в sink'и (user-facing observability).
LLM события не видит — её feedback-loop обслуживает ``MessageService``.
Поэтому у всех маршрутизируемых ошибок есть общий метод
``to_user_event``, а LLM-feedback — дополнительное side-effect
у :class:`LLMFeedbackError`.


════════════════════════════════════════════════════════════════════
  Иерархия маркеров (этот модуль)
════════════════════════════════════════════════════════════════════

::

    RoutableError                                       (abstract)
    │
    ├── UserFeedbackError[TReqId, TUserEvent]           (abstract)
    │   │   to_user_event(request_id) -> TUserEvent
    │   │
    │   ├── TerminalError                                  + цикл стоп
    │   ├── UserNoticeError                                нотис с severity
    │   │
    │   └── LLMFeedbackError[TReqId, TUserEvent,        (abstract)
    │                        TFeedback]                    + LLM feedback
    │           to_llm_feedback() -> TFeedback             (цикл идёт)
    │
    └── Retryable                                        marker-mixin

``UserFeedbackError`` и ``LLMFeedbackError`` — **декларативный
контракт**: подкласс обязан реализовать ``to_user_event`` (+
``to_llm_feedback`` для LLM-ветки). Роутер
(:class:`~boba.domain.agent.meat.error_routing.AgentErrorRouter`) не
знает конкретных подклассов — он просто делегирует этим методам через
``isinstance``-проверку семейства.

``LLMFeedbackError`` наследуется от ``UserFeedbackError`` потому что
любая LLM-feedback ошибка ТАКЖЕ показывается пользователю через sink
(событие-эхо: tool упал, LLM переформулирует → юзер видит факт). LLM
получает feedback параллельно через ``MessageService``.

Generics позволяют core не зависеть от agent-слойных типов событий.
Agent-слой привязывает ``TReqId = RequestId``, ``TUserEvent =
AgentEvent``, ``TFeedback = LLMMessage`` через промежуточные бейзы
(:class:`~boba.domain.agent.errors.AgentTerminalError`,
:class:`~boba.domain.agent.errors.AgentLLMFeedbackError`).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Generic, Literal, TypeVar

from boba.domain.core.patterns import UuId

TReqId = TypeVar("TReqId", bound=UuId)
TUserEvent = TypeVar("TUserEvent")
TFeedback = TypeVar("TFeedback")


class RoutableError(Exception, ABC):
    """Базовый класс всех **маршрутизируемых** доменных ошибок.

    Подкласс этого типа == «роутер знает, что с этим делать». Всё, что
    не наследует ``RoutableError``, считается багом и не обрабатывается.

    Делится по оси «кому адресовано сообщение»:

    - :class:`UserFeedbackError` — видит пользователь через UI-sink,
      LLM не видит;
    - :class:`LLMFeedbackError` — видит LLM через ``MessageService``.

    Сам :class:`RoutableError` абстрактен: concrete-ошибки обязаны
    унаследоваться от одного из двух семейств (или обоих — смешанные
    case-ы не предусмотрены текущим роутером) и реализовать
    соответствующие ``to_*_event`` методы.
    """


class UserFeedbackError(RoutableError, Generic[TReqId, TUserEvent], ABC):
    """Сообщение, адресованное **пользователю**.

    Пользователь увидит через UI-sink. LLM **не** видит — в
    ``MessageService`` не пишется.

    Параметры:

    - ``TReqId`` — тип идентификатора запроса (bound=:class:`UuId`).
      Agent-слой связывает с :class:`~boba.domain.agent.models.RequestId`.
    - ``TUserEvent`` — тип события, в которое ошибка превращается.
      Agent-слой связывает с :class:`~boba.domain.agent.events.AgentEvent`.
    """

    @abstractmethod
    def to_user_event(self, request_id: TReqId) -> TUserEvent:
        """Построить observability-событие для sink'ов.

        Вызывается роутером. ``request_id`` прокидывается снаружи,
        чтобы ошибка не хранила reference на контекст.

        Тело — ``raise NotImplementedError``, а не ``...``: ``Exception``
        + ``ABC`` обходит проверку ``__abstractmethods__`` на
        ``__new__`` (CPython #42188), поэтому прямое ``raise``
        инстанцирование абстрактного класса не блокирует — но
        первый же вызов метода упадёт.
        """
        raise NotImplementedError(
            f"{type(self).__name__}.to_user_event не реализован",
        )


class TerminalError(UserFeedbackError[TReqId, TUserEvent], ABC):
    """Ошибка прерывает текущий запрос и превращается в ``*Failed``-событие.

    Подкласс :class:`UserFeedbackError`: пользователь видит сообщение.
    Плюс :class:`~boba.domain.agent.meat.StopOnAnyFailure` останавливает цикл.
    """


UserNoticeSeverity = Literal["info", "warning", "error"]


class UserNoticeError(UserFeedbackError[TReqId, TUserEvent], ABC):
    """Нотис пользователю с уровнем важности — не-терминальный.

    Хранит ``message`` и ``severity`` на уровне core (универсальные поля).
    Конкретный ``to_user_event`` — в agent-слое (:class:`
    ~boba.domain.agent.errors.AgentUserNotice`), т.к. это событие
    зависит от ``AgentEvent``-типа.

    Use-cases: warnings, deprecation-нотисы, soft-rejects валидации.
    """

    def __init__(
        self, message: str, *, severity: UserNoticeSeverity = "info"
    ) -> None:
        super().__init__(message)
        self.message = message
        self.severity: UserNoticeSeverity = severity


class LLMFeedbackError(
    UserFeedbackError[TReqId, TUserEvent],
    Generic[TReqId, TUserEvent, TFeedback],
    ABC,
):
    """Ошибка с двойным эффектом: user видит event + LLM получает feedback.

    Наследуется от :class:`UserFeedbackError` (events — это канал в
    sink'и; LLM события не видит) и добавляет feedback-сторону:
    роутер пишет :meth:`to_llm_feedback` в
    :class:`~boba.domain.agent.messages.MessageService`, и LLM на
    следующей итерации видит ошибку в истории как ``LLMMessage``.
    Цикл продолжается.

    Concrete-подкласс обязан реализовать:

    - :meth:`to_user_event` — унаследованный observability-event для
      sink'ов (см. :class:`UserFeedbackError`).
    - :meth:`to_llm_feedback` — сообщение для :class:`MessageService`.

    Параметр ``TFeedback`` добавляется к ``TReqId`` / ``TUserEvent``
    родителя — agent-слой связывает с
    :class:`~boba.domain.agent.models.LLMMessage`.
    """

    @abstractmethod
    def to_llm_feedback(self) -> TFeedback:
        """Сообщение, которое роутер добавит в ``MessageService``."""
        raise NotImplementedError(
            f"{type(self).__name__}.to_llm_feedback не реализован",
        )


class Retryable(RoutableError):  # noqa: N818
    """Маркер-миксин: ошибку имеет смысл повторить.

    Ортогонален :class:`TerminalError` / :class:`LLMFeedbackError` /
    :class:`UserFeedbackError`: retry-слой ловит по этому маркеру,
    не зная конкретной причины. После исчерпания попыток ошибка летит
    дальше и обрабатывается по своему основному типу.

    Методов маршрутизации у маркера **нет**: композиция с семейством
    даёт ``to_*_event``. Пример: ``class RetryableLLMError(LLMError,
    Retryable)`` — ``LLMError`` уже реализует ``to_user_event``,
    ``Retryable`` лишь помечает, что повтор имеет смысл. В самом
    ``to_user_event`` принято читать флаг через
    ``isinstance(self, Retryable)`` для поля ``retryable`` события.

    Сам по себе без семейства бессмысленен — роутер упадёт с
    ``TypeError``.
    """
