"""Управление циклом агента: счётчик итераций и условия остановки.

Зеркало старого :mod:`boba.domain.agent.meat.loop_control` — без
изменений в семантике, но с зависимостями на ``boba_2``-типы и с
**пересозданием request'а** на каждой итерации (в отличие от старого
кода, где request жил через все итерации как shared mutable).
"""

from __future__ import annotations

from collections.abc import Iterable

from boba.domain.core.patterns import Specification, StreamSource
from boba_2.domain.agent.errors import MaxIterationsExceededError
from boba_2.domain.agent.events import (
    AgentEvent,
    GenerationDone,
    IterationStarted,
    TerminalFailure,
)
from boba_2.domain.agent.models import AgentContext
from boba_2.domain.llm.models import LLMRequest


class IterationCounterMiddleware(StreamSource[AgentContext, AgentEvent]):
    """Инкрементирует счётчик и эмитит :class:`IterationStarted`.

    При превышении ``ctx.config.max_iterations`` поднимает
    :class:`MaxIterationsExceededError`.
    :class:`~boba_2.domain.agent.meat.error_boundary.\
AgentErrorBoundaryMiddleware` конвертирует её в
    :class:`MaxIterationsReached`, а :class:`StopOnAnyFailure`
    останавливает цикл.

    **Сброс запроса.** На каждой итерации создаёт fresh
    :class:`LLMRequest` — чтобы стадии сборки запроса работали с
    чистым состоянием, а не с накоплениями прошлых итераций.
    Содержимое истории диалога на следующих итерациях восстанавливается
    из :class:`~boba_2.domain.agent.messages.MessageService` через
    :class:`~boba_2.domain.agent.meat.history.HistoryMiddleware`.
    """

    def __init__(self, inner: StreamSource[AgentContext, AgentEvent]) -> None:
        self._inner = inner

    def name(self) -> str:
        return "IterationCounter"

    def reset(self) -> None:
        self._inner.reset()

    def stream(self, ctx: AgentContext) -> Iterable[AgentEvent]:
        ctx.iteration += 1
        limit = ctx.config.max_iterations
        if ctx.iteration > limit:
            raise MaxIterationsExceededError(
                f"Исчерпан лимит итераций цикла агента: {ctx.iteration} > "
                f"{limit}. Финальный ответ не получен.",
                limit=limit,
                iteration=ctx.iteration,
            )

        ctx.request = LLMRequest()

        yield IterationStarted(
            request_id=ctx.agent_request.request_id,
            iteration=ctx.iteration,
            max_iterations=limit,
        )
        yield from self._inner.stream(ctx)


class StopOnFinished(Specification[tuple[AgentContext, AgentEvent]]):
    """Останавливает цикл, если генерация завершилась не tool_calls'ом.

    ``tool_calls`` — единственный не-терминальный ``finish_reason``:
    модель просит исполнить tool, следующая итерация подаст его
    результат. Остальные случаи (``stop`` / ``length`` /
    ``content_filter``) — конец разговора.
    """

    def check(self, candidate: tuple[AgentContext, AgentEvent]) -> bool:
        _ctx, event = candidate
        if isinstance(event, GenerationDone):
            return event.finish_reason.is_terminal
        return False


class StopOnAnyFailure(Specification[tuple[AgentContext, AgentEvent]]):
    """Останавливает цикл при любом :class:`TerminalFailure`."""

    def check(self, candidate: tuple[AgentContext, AgentEvent]) -> bool:
        _ctx, event = candidate
        return isinstance(event, TerminalFailure)
