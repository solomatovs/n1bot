"""Middleware для tools: исполнение + защита от лупов.

Tool вызывается через единственный публичный API tool-слоя —
`ToolExecutor.stream(ctx, req) -> Iterator[ToolEvent]`. По ходу
выполнения tool yield-ит `ToolProgressReported`-индикаторы (опционально)
и завершает поток одним терминальным `ToolStreamCompleted` с результатом.
`ToolToAgentConverter` маппит первое в `ToolProgress` PhaseEvent, второе
— в `ToolResultReady` ContentSnapshotEvent.

Tool'ы, написанные как обычные функции (`def f(...) -> X`), снаружи
ничем не отличаются от generator-tools: `DishkaTool` оборачивает их
результат в один `ToolStreamCompleted` — middleware видит унифицированный
поток.

Граница слоёв: tool-слой эмитит `ToolEvent` (`boba.tools.domain.events`)
и не знает про `AgentEvent`. Маппинг — задача `ToolToAgentConverter`
здесь, по аналогии с `LLMToAgentConverter` в `llm.py`.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from typing import assert_never

from boba.agent.agent import AgentContext
from boba.agent.events import (
    AgentEvent,
    FeedbackToLLMAdded,
    Severity,
    ToolArgsResolved,
    ToolCallComplete,
    ToolExecutionFailed,
    ToolExecutionStarted,
    ToolProgress,
    ToolResultReady,
)
from boba.agent.models import ToolCallFailure, ToolCallResult
from boba.llm.models import RequestId
from boba.llm.models import ToolCall as LLMToolCall
from boba.patterns import StreamSource
from boba.tools.domain import (
    ErrorResult,
    ToolContext,
    ToolEvent,
    ToolExecutionError,
    ToolId,
    ToolProgressReported,
    ToolSeverity,
    ToolStreamCompleted,
)
from boba.tools.domain import ToolCall as DomainToolCall
from boba.tools.framework import ToolExecutor

__all__ = [
    "RepeatedToolCallGuardMiddleware",
    "ToolExecutionMiddleware",
    "ToolToAgentConverter",
]


def _map_severity(severity: ToolSeverity) -> Severity:
    """ToolSeverity → agent Severity 1-к-1."""
    match severity:
        case ToolSeverity.INFO:
            return Severity.INFO
        case ToolSeverity.WARN:
            return Severity.WARN
        case ToolSeverity.ERROR:
            return Severity.ERROR
        case _:
            assert_never(severity)


class ToolToAgentConverter:
    """Stateless конвертер ToolEvent → AgentEvent.

    Симметрично к `LLMToAgentConverter`: одна точка маппинга tool-слоя
    в agent-слой. Tool-слой не знает про `AgentEvent`; agent-middleware
    не знает про детали tool-слоя, кроме контракта `ToolEvent`.

    `request_id` и `call` приходят извне (из `ToolCallComplete`), потому что
    tool-domain про них не знает (tool ничего не должен знать про
    request_id агента).
    """

    def convert(
        self,
        event: ToolEvent,
        *,
        request_id: RequestId,
        call: LLMToolCall,
    ) -> Iterator[AgentEvent]:
        match event:
            case ToolProgressReported(
                headline=headline,
                details=details,
                severity=severity,
            ):
                yield ToolProgress(
                    request_id=request_id,
                    tool_call_id=call.id,
                    tool_name=call.name,
                    headline=headline,
                    details=dict(details),
                    severity=_map_severity(severity),
                )
            case ToolStreamCompleted(result=result):
                yield ToolResultReady(
                    request_id=request_id,
                    call=call,
                    result=ToolCallResult(result=result),
                )
            case _:
                assert_never(event)


class ToolExecutionMiddleware(StreamSource[AgentContext, AgentEvent]):
    """Исполняет tool_calls после завершения inner-стрима.

    Сам tool вызывается через `ToolExecutor.stream(...)`, что даёт
    `ToolProgressReported`-события по ходу выполнения long-running
    операций (например, индексация большого confluence-space'а).
    `ToolToAgentConverter` транслирует их в `ToolProgress` AgentEvent'ы.
    """

    def __init__(
        self,
        inner: StreamSource[AgentContext, AgentEvent],
        tool_executor: ToolExecutor,
    ) -> None:
        self._inner = inner
        self._tool_executor = tool_executor
        self._converter = ToolToAgentConverter()

    def name(self) -> str:
        return "ToolExecution"

    def reset(self) -> None:
        self._inner.reset()

    def stream(self, ctx: AgentContext) -> Iterable[AgentEvent]:
        pending: list[ToolCallComplete] = []

        for event in self._inner.stream(ctx):
            yield event
            if isinstance(event, ToolCallComplete):
                pending.append(event)

        for tc in pending:
            yield from self._run_tool(tc)

    def _run_tool(
        self,
        tc: ToolCallComplete,
    ) -> Iterable[AgentEvent]:
        call = tc.call

        yield ToolExecutionStarted(
            request_id=tc.request_id,
            tool_call_id=call.id,
            tool_name=call.name,
        )
        yield ToolArgsResolved(
            request_id=tc.request_id,
            call=call,
        )

        domain_call = DomainToolCall(
            tool_id=ToolId(call.name),
            arguments=dict(call.args),
        )
        try:
            for tool_event in self._tool_executor.stream(
                ToolContext(), domain_call,
            ):
                yield from self._converter.convert(
                    tool_event, request_id=tc.request_id, call=call,
                )
        except ToolExecutionError as e:
            error = ErrorResult(message=e.message, error_kind=type(e).__name__)
            yield ToolExecutionFailed(
                request_id=tc.request_id,
                call=call,
                failure=ToolCallFailure(
                    error_kind=error.error_kind,
                    message=error.message,
                ),
            )


class RepeatedToolCallGuardMiddleware(StreamSource[AgentContext, AgentEvent]):
    """Подавляет (N+1)-й подряд идентичный ToolCallComplete."""

    def __init__(
        self,
        inner: StreamSource[AgentContext, AgentEvent],
        max_consecutive: int,
    ) -> None:
        self._inner = inner
        self._max_consecutive = max_consecutive
        self._last: tuple[str, str] | None = None
        self._count = 0

    def name(self) -> str:
        return "RepeatedToolCallGuard"

    def reset(self) -> None:
        self._last = None
        self._count = 0
        self._inner.reset()

    def stream(self, ctx: AgentContext) -> Iterable[AgentEvent]:
        for event in self._inner.stream(ctx):
            if not isinstance(event, ToolCallComplete):
                yield event
                continue

            call = event.call
            key = (call.name, call.args_json())
            if self._last == key:
                self._count += 1
            else:
                self._last = key
                self._count = 1

            if self._count > self._max_consecutive:
                message = (
                    f"Обнаружен луп: {self._count}-й подряд "
                    f"идентичный вызов '{call.name}' с "
                    f"args={call.args_json()}. Результат уже "
                    f"есть в предыдущих role='tool' сообщениях. "
                    f"Либо вызови инструмент с другими аргументами, "
                    f"либо сформулируй ответ пользователю обычным "
                    f"текстом."
                )
                error = ErrorResult(
                    message=message,
                    error_kind="RepeatedToolCallError",
                )
                yield FeedbackToLLMAdded(
                    request_id=event.request_id,
                    content=message,
                )
                yield ToolExecutionFailed(
                    request_id=event.request_id,
                    call=call,
                    failure=ToolCallFailure(
                        error_kind=error.error_kind,
                        message=error.message,
                    ),
                )
                continue

            yield event
