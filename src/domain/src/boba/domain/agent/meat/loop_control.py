"""Управление жизненным циклом цикла агента: счётчик итераций и
условия остановки (:class:`Specification`)."""

from __future__ import annotations

from collections.abc import Iterator

from boba.domain.agent.events import (
    AgentEvent,
    GenerationDone,
    GenerationFailed,
    PersistenceFailed,
    PromptFailed,
)
from boba.domain.agent.models import AgentContext
from boba.domain.core.patterns import Specification, StreamSource


class IterationCounterMiddleware(StreamSource[AgentContext, AgentEvent]):
    """
    Подсчет кол-ва итераций цикла агента.
    Увеличивает счетчик в контексте и делегирует следующему слою.
    """

    def __init__(self, inner: StreamSource[AgentContext, AgentEvent]) -> None:
        self._inner = inner

    def name(self) -> str:
        return "Counter"

    def stream(self, ctx: AgentContext) -> Iterator[AgentEvent]:
        ctx.iteration += 1
        yield from self._inner.stream(ctx)


class StopOnFinished(Specification[tuple[AgentContext, AgentEvent]]):
    """Останавливает если генерация завершена и не tool_calls."""

    def check(self, candidate: tuple[AgentContext, AgentEvent]) -> bool:
        _ctx, event = candidate

        if isinstance(event, GenerationDone):
            return event.finish_reason != "tool_calls"

        return False


class StopOnAnyFailure(Specification[tuple[AgentContext, AgentEvent]]):
    """Останавливает цикл при любом терминальном failed-событии.

    Покрывает :class:`GenerationFailed`, :class:`PromptFailed`,
    :class:`PersistenceFailed` — узкие *ToEvent middleware уже сконвертировали
    соответствующие исключения.
    """

    def check(self, candidate: tuple[AgentContext, AgentEvent]) -> bool:
        _ctx, event = candidate
        return isinstance(event, (GenerationFailed, PromptFailed, PersistenceFailed))


class StopOnMaxIterations(Specification[tuple[AgentContext, AgentEvent]]):
    """
    Останавливает если превышен лимит итераций
    """

    def check(self, candidate: tuple[AgentContext, AgentEvent]) -> bool:
        ctx, event = candidate

        if isinstance(event, GenerationDone):
            return ctx.iteration >= ctx.config.max_iterations

        return False
