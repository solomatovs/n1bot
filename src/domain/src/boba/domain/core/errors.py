"""Базовая routing-таксономия доменных ошибок.

Живёт в ``core``, потому что используется и core-сервисами
(:mod:`boba.domain.core.promt`), и agent-слоем
(:mod:`boba.domain.agent.errors`, :mod:`boba.domain.agent.history`). Конкретные агентские подклассы (LLM-иерархия,
:class:`~boba.domain.agent.errors.ToolFeedbackError`) — в
:mod:`boba.domain.agent.errors`.

Ось первичной классификации — **кому адресовано сообщение**
(user / LLM). «Терминальность» — флаг внутри user-ветки.


════════════════════════════════════════════════════════════════════
  Иерархия маркеров (этот модуль)
════════════════════════════════════════════════════════════════════

::

    RoutableError
    │
    ├── UserFeedbackError         user видит, LLM не видит
    │   ├── TerminalError            + цикл стоп
    │   │   │   (конкретные: LLMError, PromptError, HistoryError —
    │   │   │    см. agent.errors / core.promt / core.history)
    │   │
    │   └── UserNoticeError          цикл идёт, нотис с severity
    │
    ├── LLMFeedbackError          LLM видит через MessageService
    │       (конкретные: ToolFeedbackError — в agent.errors)
    │
    └── Retryable                 маркер-миксин, ортогональный

Маршрутизацией и эмитом событий занимается
:class:`~boba.domain.agent.meat.AgentErrorRouter`.
Практический гайд «какой тип бросать» — в :mod:`boba.domain.agent.errors`.
"""

from __future__ import annotations

from typing import Literal


class RoutableError(Exception):
    """Базовый класс всех **маршрутизируемых** доменных ошибок.

    Подкласс этого типа == «роутер знает, что с этим делать». Всё, что
    не наследует ``RoutableError``, считается багом и не обрабатывается.

    Делится по оси «кому адресовано сообщение»:

    - :class:`UserFeedbackError` — видит пользователь через UI-sink,
      LLM не видит;
    - :class:`LLMFeedbackError` — видит LLM через ``MessageService``.

    Признак «терминальности» (цикл стоп / цикл идёт) — вторичен;
    маркируется классом :class:`TerminalError` внутри user-ветки.
    """


class UserFeedbackError(RoutableError):
    """Сообщение, адресованное **пользователю**.

    Пользователь увидит через UI-sink. LLM **не** видит — в
    ``MessageService`` не пишется.

    Делится на:

    - :class:`TerminalError` — ошибка, после которой цикл агента
      останавливается;
    - :class:`UserNoticeError` и другие не-терминальные нотисы —
      цикл продолжается, сообщение показывается как информационное.
    """


class TerminalError(UserFeedbackError):
    """Ошибка прерывает текущий запрос и превращается в ``*Failed``-событие.

    Подкласс :class:`UserFeedbackError`: пользователь видит сообщение.
    Плюс :class:`~boba.domain.agent.meat.StopOnAnyFailure` останавливает цикл.

    Конкретные подклассы (:class:`~boba.domain.agent.errors.LLMError`,
    :class:`~boba.domain.core.promt.PromptError`,
    :class:`~boba.domain.core.history.HistoryError`) определяют, какое
    именно ``*Failed``-событие будет эмитнуто — роутер делает match-case
    по конкретному типу.
    """


UserNoticeSeverity = Literal["info", "warning", "error"]


class UserNoticeError(UserFeedbackError):
    """Нотис пользователю с уровнем важности — не-терминальный.

    Роутер эмитит ``UserNoticeReady(message=..., severity=...)``.
    Use-cases: warnings, deprecation-нотисы, soft-rejects валидации.
    """

    def __init__(
        self, message: str, *, severity: UserNoticeSeverity = "info"
    ) -> None:
        super().__init__(message)
        self.message = message
        self.severity: UserNoticeSeverity = severity


class LLMFeedbackError(RoutableError):
    """Ошибка, которую надо отдать **обратно LLM** как tool-/assistant-сообщение.

    Роутер пишет содержимое в ``MessageService`` как ``LLMMessage``
    соответствующей роли и эмитит наблюдательное событие для sink'ов. Цикл
    агента продолжается — LLM на следующей итерации увидит ошибку и
    сможет её скорректировать.

    Конкретный тип подкласса определяет формат сообщения (роль, куда
    прикрепить — ``tool_call_id`` и т.п.). Роутер делает match-case.
    """


class Retryable(RoutableError):  # noqa: N818
    """Маркер-миксин: ошибку имеет смысл повторить.

    Ортогонален :class:`TerminalError` / :class:`LLMFeedbackError` /
    :class:`UserFeedbackError`: retry-слой ловит по этому маркеру,
    не зная конкретной причины. После исчерпания попыток ошибка летит
    дальше и обрабатывается по своему основному типу.

    Примеры композиции: ``class RetryableLLMError(LLMError, Retryable)``.
    """
