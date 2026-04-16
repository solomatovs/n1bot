"""AgentLoop — оркестратор и условия остановки."""

from __future__ import annotations

from collections.abc import Iterator

from boba.domain.agent.events import AgentEvent, GenerationDone
from boba.domain.agent.models import AgentConfig, AgentContext, AgentRequest
from boba.domain.core.patterns import Loop, StopCondition, StreamSource


class AgentStopCondition(StopCondition[AgentContext, AgentEvent]):
    """Базовый тип условия остановки агента."""

    def or_(self, other: AgentStopCondition) -> AgentStopCondition:
        return _OrAgentStop(self, other)

    def and_(self, other: AgentStopCondition) -> AgentStopCondition:
        return _AndAgentStop(self, other)


class _OrAgentStop(AgentStopCondition):
    def __init__(self, left: AgentStopCondition, right: AgentStopCondition) -> None:
        self._left = left
        self._right = right

    def should_stop(self, ctx: AgentContext, event: AgentEvent) -> bool:
        return self._left.should_stop(ctx, event) or self._right.should_stop(ctx, event)


class _AndAgentStop(AgentStopCondition):
    def __init__(self, left: AgentStopCondition, right: AgentStopCondition) -> None:
        self._left = left
        self._right = right

    def should_stop(self, ctx: AgentContext, event: AgentEvent) -> bool:
        return self._left.should_stop(ctx, event) and self._right.should_stop(
            ctx, event
        )


class StopOnFinished(AgentStopCondition):
    """Останавливает если генерация завершена и не tool_calls."""

    def should_stop(self, ctx: AgentContext, event: AgentEvent) -> bool:
        return isinstance(event, GenerationDone) and event.finish_reason != "tool_calls"


class StopOnMaxIterations(AgentStopCondition):
    """Останавливает если превышен лимит итераций."""

    def should_stop(self, ctx: AgentContext, event: AgentEvent) -> bool:
        return (
            isinstance(event, GenerationDone)
            and ctx.iteration >= ctx.config.max_iterations
        )


class AgentMiddleware(StreamSource[AgentContext, AgentEvent]):
    """
    Базовый класс для любого слоя в middleware-цепочке агента.
    Terminal (адаптер) реализует produce() напрямую.
    Middleware принимает next в __init__ и делегирует ему.
    """


class AgentLoop(Loop[AgentContext, AgentEvent]):
    """
    Оркестратор агента.
    Middleware-цепочка и условия остановки передаются снаружи через DI.
    """

    def __init__(
        self,
        config: AgentConfig,
        chain: AgentMiddleware,
        stop: AgentStopCondition,
    ) -> None:
        self._config = config
        super().__init__(source=chain, stop=stop)

    def name(self) -> str:
        return "AgentLoop"

    def run(self, request: AgentRequest) -> Iterator[AgentEvent]:
        """создаёт контекст и запускает цикл."""
        yield from self.produce(
            AgentContext(
                request=request,
                config=self._config,
            )
        )
