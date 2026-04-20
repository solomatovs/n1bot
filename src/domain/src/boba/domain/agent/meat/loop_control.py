"""Управление жизненным циклом цикла агента: счётчик итераций и
условия остановки (:class:`Specification`)."""

from __future__ import annotations

from collections.abc import Iterator

from boba.domain.agent.errors import MaxIterationsExceededError
from boba.domain.agent.events import (
    AgentEvent,
    FinishReason,
    GenerationDone,
    GenerationFailed,
    MaxIterationsReached,
    PersistenceFailed,
    PromptFailed,
    RepeatedFormatFailure,
)
from boba.domain.agent.models import AgentContext
from boba.domain.core.patterns import Specification, StreamSource


class IterationCounterMiddleware(StreamSource[AgentContext, AgentEvent]):
    """
    Подсчет кол-ва итераций цикла агента.
    Увеличивает счетчик в контексте и делегирует следующему слою.

    При превышении ``ctx.config.max_iterations`` бросает
    :class:`MaxIterationsExceededError` — роутер конвертирует в
    :class:`MaxIterationsReached`, :class:`StopOnAnyFailure` останавливает
    цикл.
    """

    def __init__(self, inner: StreamSource[AgentContext, AgentEvent]) -> None:
        self._inner = inner

    def name(self) -> str:
        return "Counter"

    def stream(self, ctx: AgentContext) -> Iterator[AgentEvent]:
        ctx.iteration += 1
        limit = ctx.config.max_iterations
        if ctx.iteration > limit:
            raise MaxIterationsExceededError(
                f"Исчерпан лимит итераций цикла агента: {ctx.iteration} > "
                f"{limit}. Финальный ответ не получен.",
                limit=limit,
                iteration=ctx.iteration,
            )
        yield from self._inner.stream(ctx)


class StopOnFinished(Specification[tuple[AgentContext, AgentEvent]]):
    """Останавливает если генерация завершена и не tool_calls."""

    def check(self, candidate: tuple[AgentContext, AgentEvent]) -> bool:
        _ctx, event = candidate

        if isinstance(event, GenerationDone):
            return event.finish_reason != FinishReason.TOOL_CALLS

        return False


class StopOnAnyFailure(Specification[tuple[AgentContext, AgentEvent]]):
    """Останавливает цикл при любом терминальном failed-событии.

    Покрывает :class:`GenerationFailed`, :class:`PromptFailed`,
    :class:`PersistenceFailed`, :class:`MaxIterationsReached`,
    :class:`RepeatedFormatFailure` — узкие *ToEvent middleware уже
    сконвертировали соответствующие исключения.
    """

    def check(self, candidate: tuple[AgentContext, AgentEvent]) -> bool:
        _ctx, event = candidate
        return isinstance(
            event,
            (
                GenerationFailed,
                PromptFailed,
                PersistenceFailed,
                MaxIterationsReached,
                RepeatedFormatFailure,
            ),
        )
