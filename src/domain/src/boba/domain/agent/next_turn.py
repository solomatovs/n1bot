"""NextTurnIntent — декларативное описание "почему запускается следующая итерация".

НАБРОСОК. Не подключен в композицию. Задача — увидеть форму, прежде
чем переделывать ``ToolExecutionMiddleware`` / ``AgentErrorRouter`` /
``UserPromptMiddleware``.

════════════════════════════════════════════════════════════════════
  Идея
════════════════════════════════════════════════════════════════════

Сейчас "что сказать LLM на следующем ходе" размазано по трём местам:

- ``UserPromptMiddleware`` пересобирает ``user_message`` из провайдеров
  каждую итерацию (включая non-first, когда сырой user-query уже не
  актуален — актуальны результаты тулов);
- ``ToolExecutionMiddleware`` пишет ``role="tool"`` прямо в
  :class:`MessageService`;
- ``AgentErrorRouter.route`` пишет ``err.to_llm_feedback()`` прямо в
  :class:`MessageService`.

Producer'ы независимы и не координируют приоритет: если в одной
итерации успели отработать тулы и выскочила ошибка уровнем выше —
"главное сообщение к LLM" определяется порядком middleware, а не
декларативным правилом.

``NextTurnIntent`` — единый sum-тип, который каждый producer
декларирует через ``ctx.set_next_turn(...)``. В начале следующей
итерации :class:`~boba.domain.agent.meat.next_turn.\
NextTurnApplyMiddleware` читает intent и транслирует его в builder
(+ при необходимости в :class:`MessageService`).

════════════════════════════════════════════════════════════════════
  Почему HYBRID, а не pure-declarative
════════════════════════════════════════════════════════════════════

Pure-declarative: intent несёт ВСЕ данные (в т.ч. tool-messages),
producer в MessageService не пишет, единственный writer —
apply-middleware.

Проблема pure-варианта — **слияние при конфликте**. Если в одной
итерации сработали и ``ToolResultsIntent`` (prio=20), и
``LLMFeedbackIntent`` (prio=30) — при pure-declarative данные тулов
теряются вместе с проигравшим intent'ом.

Hybrid: :class:`MessageService` остаётся append-log'ом (source of
truth для истории); intent — лёгкий маркер "почему ещё один виток" +
узкий write только для feedback-сообщения, которое больше нигде не
рождается. Тогда победа ``LLMFeedbackIntent`` над ``ToolResultsIntent``
безопасна: tool-messages уже в сервисе, feedback добавится сверху.

════════════════════════════════════════════════════════════════════
  Политика слияния
════════════════════════════════════════════════════════════════════

``ctx.set_next_turn(intent)`` — "last-wins при равном приоритете,
выигрывает больший приоритет". Явные уровни:

- 10: :class:`UserQueryIntent` — стартовый, ставится в ``Agent.run``.
- 20: :class:`ToolResultsIntent` — producer ``ToolExecutionMiddleware``.
- 30: :class:`LLMFeedbackIntent` — producer ``AgentErrorRouter`` для
  ошибок уровня LLM/prompt/iteration.

Разницы между "tool-feedback внутри batch'а тулов" и "общий
LLMFeedback" больше нет: внутри batch'а ``ToolFeedbackError`` всё ещё
пишется в :class:`MessageService` как ``role="tool"``, но НЕ ставит
``LLMFeedbackIntent`` — она часть текущего tool-batch'а. Это требует
перевести ``AgentErrorRouter.run_batch`` в режим "tool-aware": в tool-
контексте ``to_llm_feedback`` пишется немедленно, intent не ставится.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from boba.domain.llm.models import LLMMessage


class NextTurnIntent(ABC):
    """Что должно быть сказано LLM на следующей итерации.

    Приоритет используется :meth:`AgentContext.set_next_turn` для
    слияния конкурирующих деклараций внутри одной итерации.
    """

    @abstractmethod
    def priority(self) -> int: ...


@dataclass(frozen=True)
class UserQueryIntent(NextTurnIntent):
    """Первая итерация: начальный вопрос пользователя.

    Ставится :class:`~boba.domain.agent.meat.agent.Agent` при старте.
    ``query`` — сырой текст из ``AgentRequest.query``; apply-middleware
    записывает его как ``role="user"`` в :class:`MessageService` один
    раз и выставляет ``ctx.request.user_message``.
    """

    query: str

    def priority(self) -> int:
        return 10


@dataclass(frozen=True)
class ToolResultsIntent(NextTurnIntent):
    """Маркер: следующая итерация крутится из-за tool_calls.

    Фактические ``role="tool"`` записи УЖЕ в :class:`MessageService`
    (их пишет ``ToolExecutionMiddleware``). Intent здесь — маркер для
    apply-middleware, чтобы тот:

    1. не перезаписал sticky user_message сырым запросом пользователя;
    2. мог эмитить событие "итерация из-за тулов" для наблюдаемости.

    ``tool_call_count`` — только для логов/событий.
    """

    tool_call_count: int

    def priority(self) -> int:
        return 20


@dataclass(frozen=True)
class LLMFeedbackIntent(NextTurnIntent):
    """Ошибка, классифицированная как LLMFeedbackError.

    Несёт ответственность за запись feedback-сообщения в
    :class:`MessageService` — это единственное место в системе, где
    оно рождается. ``message`` — готовый результат
    ``err.to_llm_feedback()``; apply-middleware добавит его в сервис
    и восстановит sticky user_message в builder'е.

    Важно: если этот intent побеждает ``ToolResultsIntent`` при
    слиянии — tool-messages не теряются, т.к. они уже в сервисе.
    """

    message: LLMMessage
    error_kind: str

    def priority(self) -> int:
        return 30
