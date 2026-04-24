"""TurnEffect — атомарная "что-то произошло в диалоге" декларация.

Эффект — единственный способ дописать сообщение в
:class:`MessageService`. Producer'ы (ToolExecution, AgentErrorRouter,
Agent.run) не пишут в сервис напрямую: они декларируют эффект в
:class:`~boba.domain.agent.turn.trigger.TurnTrigger` через
``AgentContext.declare``. Apply происходит один раз в
:meth:`TurnSpec.initial` на следующей итерации.

Расширяемость: новый сценарий = новый подкласс :class:`TurnEffect`.
Reducer'ы и резолвер не надо трогать — их интерфейс не зависит от
конкретного варианта эффекта.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from boba.domain.agent.messages import MessageService
from boba.domain.llm.models import LLMMessage


class TurnEffect(ABC):
    """Одно доменное событие, которое пишется в диалог."""

    @abstractmethod
    def apply(self, svc: MessageService) -> None:
        """Записать payload эффекта в историю."""


@dataclass(frozen=True)
class UserQueryEffect(TurnEffect):
    """Начальный user-query (первая итерация).

    ``content`` — уже обогащённый фабрикой текст (raw query +
    workspace-prelude + что там провайдеры добавили). Сборка
    текста — ответственность producer'а (``Agent.run``), а не
    эффекта — так эффект остаётся data-only и тестируется без DI.
    """

    content: str

    def apply(self, svc: MessageService) -> None:
        svc.add(LLMMessage(role="user", content=self.content))


@dataclass(frozen=True)
class ToolResultEffect(TurnEffect):
    """Один результат tool-call (успех или ошибка — всё равно role=tool).

    Ошибки тулов тоже доходят до LLM как ``role="tool"`` с текстом
    ошибки — это всё ещё "результат вызова инструмента", просто
    с негативным payload'ом.
    """

    tool_call_id: str
    content: str

    def apply(self, svc: MessageService) -> None:
        svc.add(
            LLMMessage(
                role="tool",
                content=self.content,
                tool_call_id=self.tool_call_id,
            ),
        )


@dataclass(frozen=True)
class LLMFeedbackEffect(TurnEffect):
    """Ошибка уровня LLM/prompt/iteration, классифицированная как feedback.

    ``message`` — готовый :class:`LLMMessage` из
    ``err.to_llm_feedback()``. ``error_kind`` — имя класса ошибки
    (для трейсинга/событий, сам :meth:`apply` его не использует).
    """

    message: LLMMessage
    error_kind: str

    def apply(self, svc: MessageService) -> None:
        svc.add(self.message)
