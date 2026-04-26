"""Маркеры доменных ошибок для полиморфной маршрутизации.

Идея: concrete-ошибка наследуется от :class:`RoutableError` и миксует
любое подмножество маркеров-эффектов. Роутер читает маркеры
независимо и суммирует эффекты — никакой жёсткой иерархии между
маркерами нет.

Маркеры:

:class:`UserFeedbackError[TReqId, TUserEvent]`
    Ошибка превращается в observability-событие для sink'ов.
    Реализует :meth:`to_user_feedback`.

:class:`LLMFeedbackError[TFeedback]`
    Ошибка добавляется в историю (MessageService) как сообщение для
    LLM — на следующей итерации модель увидит фидбек.
    Реализует :meth:`to_llm_feedback`.

:class:`TerminalError[TReqId, TUserEvent]`
    Специализация :class:`UserFeedbackError`: ошибка останавливает
    агентский цикл. Концепт «остановить цикл» кодируется *типом
    возвращаемого события* (наследник ``TerminalFailure``), поэтому
    :class:`TerminalError` обязывает реализовать :meth:`to_user_feedback`
    и задокументирован как возвращающий терминальное событие —
    никакого отдельного маркера без метода нет.

Комбинировать можно произвольно::

    class MaxIter(TerminalError[RequestId, AgentEvent]):
        def to_user_feedback(self, rid): return MaxIterationsReached(...)

    class ToolFail(
        UserFeedbackError[RequestId, AgentEvent],
        LLMFeedbackError[LLMMessage],
    ):
        def to_user_feedback(self, rid): ...
        def to_llm_feedback(self): ...

    class LLMConn(LLMFeedbackError[LLMMessage]):
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
    :class:`TerminalError`.
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
    def to_user_feedback(self, request_id: TReqId) -> TUserEvent:
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


class TerminalError(UserFeedbackError[TReqId, TUserEvent], ABC):
    """Маркер: ошибка останавливает агентский цикл.

    Специализация :class:`UserFeedbackError`. «Терминальность» —
    свойство *события*, а не ошибки: цикл останавливается, когда в
    stream выходит наследник ``TerminalFailure``. Поэтому
    :class:`TerminalError` обязывает реализовать
    :meth:`to_user_feedback`, который обязан вернуть подкласс
    ``TerminalFailure``.

    Контракт на тип возврата выражен документально (в core-слое нет
    ссылки на ``TerminalFailure`` из agent-слоя), но covariance возврата
    в Python делает сужение статически проверяемым: подкласс может
    аннотировать ``-> TerminalFailure`` и type-checker примет.
    """
