"""Specification[AgentEvent] — переиспользуемые предикаты по событиям агента."""

from __future__ import annotations

from boba.agent.events import AgentEventBase, EventCategory
from boba.patterns import Specification

__all__ = ["IsContentDelta"]


class IsContentDelta(Specification[AgentEventBase]):
    """Истинно для инкрементальных чанков (`category == content_delta`).

    Комбинируется через `and_/or_/not_` стандартными средствами Specification.
    Используется журналом истории, чтобы пропускать chunk-события и хранить
    только агрегированные снапшоты/переходы фаз.
    """

    def check(self, candidate: AgentEventBase) -> bool:
        return candidate.category == EventCategory.CONTENT_DELTA
