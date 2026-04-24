"""Маркеры доменных ошибок для полиморфной маршрутизации.

Идея: concrete-ошибка наследуется от :class:`RoutableError` и миксует
любое подмножество маркеров-эффектов. Роутер читает маркеры
независимо и суммирует эффекты — никакой жёсткой иерархии между
маркерами нет.

Маркеры:

:class:`UserFeedbackError[TReqId, TUserEvent]`
    Ошибка превращается в observability-событие для sink'ов.
    Реализует :meth:`to_event`.

:class:`LLMFeedbackError[TFeedback]`
    Ошибка добавляется в историю (MessageService) как сообщение для
    LLM — на следующей итерации модель увидит фидбек.
    Реализует :meth:`to_llm_feedback`.

:class:`TerminalError`
    Чистый маркер. Ошибка должна остановить цикл. Роутер гарантирует,
    что для такой ошибки в stream выйдет событие из семейства
    ``TerminalFailure``: либо собственное через ``to_event`` (если
    ошибка также :class:`UserFeedbackError`), либо generic.

:class:`Retryable`
    Чистый маркер. Retry-middleware ловит ошибку по маркеру и
    выполняет повтор. После исчерпания попыток ошибка уходит наверх
    и маршрутизируется по остальным маркерам.

Комбинировать можно произвольно::

    class MaxIter(UserFeedbackError[RequestId, AgentEvent], TerminalError):
        def to_event(self, rid): return MaxIterationsReached(...)

    class ToolFail(
        UserFeedbackError[RequestId, AgentEvent],
        LLMFeedbackError[LLMMessage],
    ):
        def to_event(self, rid): ...
        def to_llm_feedback(self): ...

    class LLMConn(LLMFeedbackError[LLMMessage], Retryable):
        def to_llm_feedback(self): ...

Прямое наследование от :class:`RoutableError` без маркеров валидно —
роутер обработает такую ошибку как no-op (ничего не эмитит, цикл
не прерывает).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Generic, TypeVar

TReqId = TypeVar("TReqId")
TUserEvent = TypeVar("TUserEvent")
TFeedback = TypeVar("TFeedback")


class RoutableError(Exception):
    """Базовый класс всех маршрутизируемых доменных ошибок.

    Сам по себе не несёт эффектов — служит контрактом «эту ошибку
    обрабатывает роутер». Эффекты добавляются миксами маркеров:
    :class:`UserFeedbackError`, :class:`LLMFeedbackError`,
    :class:`TerminalError`, :class:`Retryable`.
    """


class UserFeedbackError(RoutableError, Generic[TReqId, TUserEvent], ABC):
    """Маркер: ошибка превращается в событие для sink'ов.

    Параметры:

    - ``TReqId`` — тип идентификатора запроса.
    - ``TUserEvent`` — тип события, в которое ошибка превращается
      (обычно union событий слоя).

    ``request_id`` прокидывается снаружи — ошибка не хранит reference
    на контекст.
    """

    @abstractmethod
    def to_event(self, request_id: TReqId) -> TUserEvent:
        """Построить observability-событие для stream."""
        ...


class LLMFeedbackError(RoutableError, Generic[TFeedback], ABC):
    """Маркер: ошибка добавляется в историю как сообщение для LLM.

    Параметр ``TFeedback`` — тип сообщения в истории (обычно
    ``LLMMessage``-like). Роутер пишет результат :meth:`to_llm_feedback`
    в :class:`MessageService` и на следующей итерации LLM увидит
    фидбек как часть контекста.
    """

    @abstractmethod
    def to_llm_feedback(self) -> TFeedback:
        """Сообщение, которое роутер добавит в историю."""
        ...


class TerminalError(RoutableError):
    """Маркер: ошибка останавливает агентский цикл.

    Чистый маркер — без методов. Роутер гарантирует, что для
    :class:`TerminalError` в stream выйдет событие из семейства
    ``TerminalFailure``:

    - если ошибка также :class:`UserFeedbackError` — :meth:`to_event`
      должен вернуть подкласс ``TerminalFailure``;
    - если :class:`UserFeedbackError` не миксован — роутер эмитит
      generic ``TerminalFailure`` сам.
    """


class Retryable(RoutableError):  # noqa: N818
    """Маркер: ошибку имеет смысл повторить.

    Чистый маркер — без методов. Retry-middleware ловит по маркеру
    и выполняет повтор без знания конкретной причины. После
    исчерпания попыток ошибка уходит выше и маршрутизируется по
    остальным маркерам.
    """
