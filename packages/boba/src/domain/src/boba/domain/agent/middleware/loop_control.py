from __future__ import annotations

from collections.abc import Iterable

from boba.domain.agent.errors import MaxIterationsExceededError
from boba.domain.agent.events import (
    AgentEvent,
    GenerationDone,
    IterationStarted,
    Terminal,
)
from boba.domain.agent.models import AgentContext
from boba.domain.core.patterns import Specification, StreamSource


class IterationCounterMiddleware(StreamSource[AgentContext, AgentEvent]):
    """Инкрементирует счётчик итераций; MaxIterationsExceededError при превышении."""

    def __init__(self, inner: StreamSource[AgentContext, AgentEvent]) -> None:
        self._inner = inner

    def name(self) -> str:
        return "IterationCounter"

    def reset(self) -> None:
        self._inner.reset()

    def stream(self, ctx: AgentContext) -> Iterable[AgentEvent]:
        ctx.iteration += 1

        if ctx.iteration > ctx.config.max_iterations:
            raise MaxIterationsExceededError(
                f"Исчерпан лимит итераций цикла агента: {ctx.iteration} > "
                f"{ctx.config.max_iterations}. Финальный ответ не получен.",
                limit=ctx.config.max_iterations,
                iteration=ctx.iteration,
            )

        yield IterationStarted(
            request_id=ctx.agent_request.request_id,
            iteration=ctx.iteration,
            max_iterations=ctx.config.max_iterations,
        )
        yield from self._inner.stream(ctx)


class StopOnFinished(Specification[tuple[AgentContext, AgentEvent]]):
    """Останавливает цикл при terminal finish_reason от LLM."""

    def check(self, candidate: tuple[AgentContext, AgentEvent]) -> bool:
        _ctx, event = candidate
        if isinstance(event, GenerationDone):
            return event.finish_reason.is_terminal
        return False


class StopOnAnyFailure(Specification[tuple[AgentContext, AgentEvent]]):
    """Останавливает цикл при любом Terminal-событии."""

    def check(self, candidate: tuple[AgentContext, AgentEvent]) -> bool:
        _ctx, event = candidate
        return isinstance(event, Terminal)
