"""TurnTrigger — упорядоченный список TurnEffect + теги.

Накапливается в ``AgentContext._pending`` producer'ами в течение
итерации через :meth:`AgentContext.declare`; консьюмится в начале
следующей итерации через :meth:`AgentContext.consume_trigger`.

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
