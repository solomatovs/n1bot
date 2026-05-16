from __future__ import annotations

from collections.abc import Iterable

from pydantic import BaseModel, ConfigDict, Field

from boba.agent.agent import AgentContext
from boba.agent.errors import MaxIterationsExceededError
from boba.agent.events import (
    AgentEvent,
    GenerationDone,
    IterationStarted,
    TerminalEvent,
)
from boba.patterns import Specification, StreamSource


class IterationCounterConfig(BaseModel):
    """Конфиг bootstrap-параметров `IterationCounterMiddleware`."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    max_iterations: int = Field(
        default=20,
        ge=1,
        description="Жёсткий потолок числа итераций агента в одной сессии.",
    )


class IterationCounterMiddleware(StreamSource[AgentContext, AgentEvent]):
    """
    Счётчик итераций
    Прерывает агентский цикл по достижении max_iterations
    через событие MaxIterationsExceededError"""

    def __init__(
        self,
        inner: StreamSource[AgentContext, AgentEvent],
        max_iterations: int,
    ) -> None:
        self._inner = inner
        self._max_iterations = max_iterations
        self._iteration = 0

    def name(self) -> str:
        return "IterationCounter"

    def reset(self) -> None:
        self._iteration = 0
        self._inner.reset()

    def stream(self, ctx: AgentContext) -> Iterable[AgentEvent]:
        self._iteration += 1

        if self._iteration > self._max_iterations:
            raise MaxIterationsExceededError(
                f"Исчерпан лимит итераций цикла агента: {self._iteration} > "
                f"{self._max_iterations}. Финальный ответ не получен.",
                limit=self._max_iterations,
                iteration=self._iteration,
            )

        yield IterationStarted(
            request_id=ctx.request_id,
            iteration_count=self._iteration,
            max_iterations=self._max_iterations,
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
