"""Middleware для tools: исполнение + защита от лупов."""

from __future__ import annotations

from collections.abc import Iterable

from boba.agent.agent import AgentContext
from boba.agent.events import (
    AgentEvent,
    ToolCallMessage,
    ToolExecutionFailed,
    ToolExecutionStarted,
    ToolResultReady,
)
from boba.agent.models import ToolCallFailure, ToolCallResult
from boba.patterns import StreamSource
from boba.tools.domain import (
    ErrorResult,
    ToolContext,
    ToolExecutionError,
    ToolId,
)
from boba.tools.domain import ToolCall as DomainToolCall
from boba.tools.framework import ToolExecutor


class ToolExecutionMiddleware(StreamSource[AgentContext, AgentEvent]):
    """Исполняет tool_calls после завершения inner-стрима."""

    def __init__(
        self,
        inner: StreamSource[AgentContext, AgentEvent],
        tool_executor: ToolExecutor,
    ) -> None:
        self._inner = inner
        self._tool_executor = tool_executor

    def name(self) -> str:
        return "ToolExecution"

    def reset(self) -> None:
        self._inner.reset()

    def stream(self, ctx: AgentContext) -> Iterable[AgentEvent]:
        pending: list[ToolCallMessage] = []

        for event in self._inner.stream(ctx):
            yield event

            # накапливаем ToolCall сообщения
            # что бы следующим шагом выполнить их
            if isinstance(event, ToolCallMessage):
                pending.append(event)

        # выполняем ToolCal's
        for tc in pending:
            yield from self._run_tool(tc)

    def _run_tool(
        self,
        tc: ToolCallMessage,
    ) -> Iterable[AgentEvent]:
        call = tc.call

        yield ToolExecutionStarted(
            request_id=tc.request_id,
            call=call,
        )

        try:
            result = self._tool_executor.execute(
                ToolContext(),
                DomainToolCall(
                    tool_id=ToolId(call.name),
                    arguments=dict(call.args),
                ),
            )

            yield ToolResultReady(
                request_id=tc.request_id,
                call=call,
                result=ToolCallResult(result=result),
            )
        except ToolExecutionError as e:
            yield ToolExecutionFailed(
                request_id=tc.request_id,
                call=call,
                failure=ToolCallFailure(
                    error_kind=type(e).__name__,
                    message=e.message,
                ),
            )


class RepeatedToolCallGuardMiddleware(StreamSource[AgentContext, AgentEvent]):
    """Подавляет (N+1)-й подряд идентичный ToolCallMessage."""

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
            if not isinstance(event, ToolCallMessage):
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
