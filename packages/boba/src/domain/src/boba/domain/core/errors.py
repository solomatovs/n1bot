"""Маркеры доменных ошибок для полиморфной маршрутизации.

Concrete-ошибка наследуется от RoutableError и миксует подмножество
маркеров; роутер читает маркеры независимо и суммирует эффекты.

- UserFeedbackError — превращается в observability-событие (to_user_feedback).
- LLMFeedbackError — добавляется в историю как сообщение для LLM (to_llm_feedback).
- TerminalError — специализация UserFeedbackError, останавливает цикл.

Прямой наследник RoutableError без маркеров — no-op для роутера.
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
    UserFeedbackError, LLMFeedbackError,
    TerminalError.
    """


class UserFeedbackError(RoutableError, Generic[TReqId, TUserEvent], ABC):
    """Маркер: ошибка превращается в событие для sink'ов.

    Параметры:

    - TReqId — тип идентификатора запроса.
    - TUserEvent — тип события, в которое ошибка превращается
      (обычно union событий слоя).

    request_id прокидывается снаружи — ошибка не хранит reference
    на контекст.
    """

    @abstractmethod
    def to_user_feedback(self, request_id: TReqId) -> TUserEvent:
        """Построить observability-событие для stream."""
        ...


class LLMFeedbackError(RoutableError, Generic[TFeedback], ABC):
    """Маркер: ошибка добавляется в историю как сообщение для LLM.

    Параметр TFeedback — тип сообщения в истории (обычно
    LLMMessage-like). Роутер пишет результат to_llm_feedback
    в MessageService и на следующей итерации LLM увидит
    фидбек как часть контекста.
    """

    @abstractmethod
    def to_llm_feedback(self) -> TFeedback:
        """Сообщение, которое роутер добавит в историю."""
        ...


class TerminalError(UserFeedbackError[TReqId, TUserEvent], ABC):
    """Маркер: ошибка останавливает агентский цикл.

    Специализация UserFeedbackError. «Терминальность» —
    свойство *события*, а не ошибки: цикл останавливается, когда в
    stream выходит наследник TerminalFailure. Поэтому
    TerminalError обязывает реализовать
    to_user_feedback, который обязан вернуть подкласс
    TerminalFailure.

    Контракт на тип возврата выражен документально (в core-слое нет
    ссылки на TerminalFailure из agent-слоя), но covariance возврата
    в Python делает сужение статически проверяемым: подкласс может
    аннотировать -> TerminalFailure и type-checker примет.
    """
