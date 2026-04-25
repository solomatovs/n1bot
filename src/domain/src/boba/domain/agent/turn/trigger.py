"""TurnTrigger — упорядоченный список TurnEffect + теги.

Накапливается в :class:`TurnTriggerQueue` producer'ами в течение
итерации через :meth:`TurnTriggerQueue.declare`; консьюмится в начале
следующей итерации через :meth:`TurnTriggerQueue.consume`.

Порядок эффектов сохраняется: ``merged_with`` — конкатенация. Это
защищает от потери данных при конкурентной декларации (tool-batch
успел дописать ToolResultEffect'ы, следом прилетел LLMFeedbackEffect
из middleware выше — всё попадает в LLM одним триггером).

Теги — frozenset строк ("user_query", "tool_result", "feedback",
произвольные пользовательские). Reducer'ы могут смотреть на теги,
чтобы менять поведение (ниже temperature при "feedback", другой
system prompt при "retry" и т.п.), но не обязаны — базовые reducer'ы
тэги игнорируют.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from boba.domain.agent.turn.effects import TurnEffect


@dataclass(frozen=True)
class TurnTrigger:
    effects: tuple[TurnEffect, ...] = ()
    tags: frozenset[str] = field(default_factory=frozenset)

    def merged_with(self, other: TurnTrigger) -> TurnTrigger:
        return TurnTrigger(
            effects=self.effects + other.effects,
            tags=self.tags | other.tags,
        )

    def is_empty(self) -> bool:
        return not self.effects and not self.tags


class TurnTriggerQueue:
    """Инбокс эффектов следующего turn'а.

    Протокол: **many writers, one reader**.

    - Producer'ы (middleware'ы внутри итерации) вызывают :meth:`declare`
      сколько угодно раз. Эффекты накапливаются в порядке деклараций,
      теги объединяются — потерь при конкурентной декларации разных
      producer'ов нет.
    - Единственный consumer (:class:`~boba.domain.agent.meat.llm.\
LLMInvokeMiddleware`) один раз за итерацию вызывает :meth:`consume`:
      получает накопленный :class:`TurnTrigger` и очищает очередь.
    """

    def __init__(self) -> None:
        self._pending: TurnTrigger = TurnTrigger()

    def declare(self, effect: TurnEffect, *tags: str) -> None:
        """Append эффект в очередь."""
        self._pending = self._pending.merged_with(
            TurnTrigger(effects=(effect,), tags=frozenset(tags)),
        )

    def consume(self) -> TurnTrigger:
        """Вернуть накопленный trigger и очистить очередь (одноразовое чтение)."""
        trigger, self._pending = self._pending, TurnTrigger()
        return trigger
