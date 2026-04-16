"""Условия остановки AgentLoop."""

from __future__ import annotations

from boba.domain.agent.events import AgentEvent, GenerationDone
from boba.domain.agent.models import AgentContext
from boba.domain.core.patterns import StopCondition


class AgentStopCondition(StopCondition[AgentContext, AgentEvent]):
    """Базовый тип условия остановки агента."""


class StopOnFinished(AgentStopCondition):
    """Останавливает если генерация завершена и не tool_calls."""

    def should_stop(self, ctx: AgentContext, event: AgentEvent) -> bool:
        return isinstance(event, GenerationDone) and event.finish_reason != "tool_calls"


class StopOnMaxIterations(AgentStopCondition):
    """Останавливает если превышен лимит итераций."""

    def should_stop(self, ctx: AgentContext, event: AgentEvent) -> bool:
        return isinstance(event, GenerationDone) and ctx.iteration >= ctx.config.max_iterations
