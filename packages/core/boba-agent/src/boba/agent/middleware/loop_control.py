from __future__ import annotations

from collections.abc import Iterable

from boba.agent.errors import MaxIterationsExceededError
from boba.agent.events import (
    AgentEvent,
    GenerationDone,
    IterationStarted,
    TerminalEvent,
)
from boba.agent.orchestrator import AgentContext
from boba.patterns import Specification, StreamSource


class IterationCounterMiddleware(StreamSource[AgentContext, AgentEvent]):
    """Инкрементирует счётчик итераций; MaxIterationsExceededError при превышении."""

    def __init__(self, inner: StreamSource[AgentContext, AgentEvent]) -> None:
        self._inner = inner
        self._iteration = 0

    def name(self) -> str:
        return "IterationCounter"

    def reset(self) -> None:
        self._iteration = 0
        self._inner.reset()

    def stream(self, ctx: AgentContext) -> Iterable[AgentEvent]:
        self._iteration += 1

        if self._iteration > ctx.config.max_iterations:
            raise MaxIterationsExceededError(
                f"Исчерпан лимит итераций цикла агента: {self._iteration} > "
                f"{ctx.config.max_iterations}. Финальный ответ не получен.",
                limit=ctx.config.max_iterations,
                iteration=self._iteration,
            )

        yield IterationStarted(
            request_id=ctx.request.request_id,
            iteration_count=self._iteration,
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
    """Останавливает цикл при любом TerminalEvent."""

    def check(self, candidate: tuple[AgentContext, AgentEvent]) -> bool:
        _ctx, event = candidate
        return isinstance(event, TerminalEvent)
