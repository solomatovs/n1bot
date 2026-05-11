"""Middleware для tools: исполнение + защита от лупов."""

from __future__ import annotations

from collections.abc import Iterable

from boba.agent.events import (
    AgentEvent,
    FeedbackToLLMAdded,
    ToolCallComplete,
    ToolExecutionFailed,
    ToolExecutionStarted,
    ToolResultReady,
)
from boba.agent.messages import MessageWriter
from boba.agent.models import ToolCallFailure, ToolCallResult
from boba.agent.orchestrator import AgentContext
from boba.llm.models import ToolResultMessage
from boba.patterns import StreamSource
from boba.tools.domain import ToolCall as DomainToolCall
from boba.tools.domain import (
    ToolContext,
    ToolExecutionError,
    ToolId,
    ToolResultVisitor,
)
from boba.tools.framework import ToolsService


class ToolExecutionMiddleware(StreamSource[AgentContext, AgentEvent]):
    """Исполняет tool_calls после завершения inner-стрима."""

    def __init__(
        self,
        inner: StreamSource[AgentContext, AgentEvent],
        tools_service: ToolsService,
        tool_ctx: ToolContext,
        writer: MessageWriter,
        visitor: ToolResultVisitor[str],
    ) -> None:
        self._inner = inner
        self._tools_service = tools_service
        self._tool_ctx = tool_ctx
        self._writer = writer
        self._visitor = visitor

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
            call=call,
        )

        try:
            result = self._tools_service.execute(
                self._tool_ctx,
                DomainToolCall(tool_id=ToolId(call.name), arguments=dict(call.args)),
            )
        except ToolExecutionError as e:
            self._writer.add(
                ToolResultMessage(
                    tool_call_id=call.id,
                    content=e.message,
                    success=False,
                ),
            )
            yield ToolExecutionFailed(
                request_id=tc.request_id,
                call=call,
                failure=ToolCallFailure(
                    error_kind=type(e).__name__,
                    message=e.message,
                ),
            )
            return

        rendered = result.accept(self._visitor)
        self._writer.add(
            ToolResultMessage(tool_call_id=call.id, content=rendered),
        )
        yield ToolResultReady(
            request_id=tc.request_id,
            call=call,
            result=ToolCallResult(content=rendered),
        )


class RepeatedToolCallGuardMiddleware(StreamSource[AgentContext, AgentEvent]):
    """Подавляет (N+1)-й подряд идентичный ToolCallComplete."""

    def __init__(
        self,
        inner: StreamSource[AgentContext, AgentEvent],
        max_consecutive: int,
        writer: MessageWriter,
    ) -> None:
        self._inner = inner
        self._max_consecutive = max_consecutive
        self._writer = writer
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
                self._writer.add(
                    ToolResultMessage(
                        tool_call_id=call.id,
                        content=message,
                        success=False,
                    ),
                )
                yield FeedbackToLLMAdded(
                    request_id=event.request_id,
                    content=message,
                )
                yield ToolExecutionFailed(
                    request_id=event.request_id,
                    call=call,
                    failure=ToolCallFailure(
                        error_kind="RepeatedToolCallError",
                        message=message,
                    ),
                )
                continue

            yield event
