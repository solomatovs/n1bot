"""Specification[AgentEvent] — переиспользуемые предикаты по событиям агента."""

from __future__ import annotations

from boba.agent.events import AgentEvent, ContentDelta
from boba.patterns import Specification

__all__ = ["IsContentDelta"]


class IsContentDelta(Specification[AgentEvent]):
    """Истинно для инкрементальных чанков (ThinkingToken / AnswerToken / ...).

    Комбинируется через `and_/or_/not_` стандартными средствами Specification.
    Используется журналом истории, чтобы пропускать chunk-события и хранить
    только агрегированные снапшоты/переходы фаз.
    """

    def check(self, candidate: AgentEvent) -> bool:
        return isinstance(candidate, ContentDelta)
