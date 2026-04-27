"""Middleware для tools: исполнение + защита от лупов."""

from __future__ import annotations

import json
from collections.abc import Iterable

from boba.domain.agent.dialogue_writer import DialogueWriter
from boba.domain.agent.events import (
    AgentEvent,
    FeedbackToLLMAdded,
    ToolCallComplete,
    ToolExecutionFailed,
    ToolExecutionStarted,
    ToolResultReady,
)
from boba.domain.agent.models import AgentContext
from boba.domain.agent.payloads import ToolCallFailure, ToolCallResult
from boba.domain.core.patterns import StreamSource
from boba.domain.core.tools import (
    ToolCall as DomainToolCall,
)
from boba.domain.core.tools import (
    ToolContext,
    ToolExecutionError,
    ToolId,
    ToolsService,
)


class ToolExecutionMiddleware(StreamSource[AgentContext, AgentEvent]):
    """Исполняет tool_calls после завершения inner-стрима."""

    def __init__(
        self,
        inner: StreamSource[AgentContext, AgentEvent],
        tools_service: ToolsService,
        tool_ctx: ToolContext,
        writer: DialogueWriter,
    ) -> None:
        self._inner = inner
        self._tools_service = tools_service
        self._tool_ctx = tool_ctx
        self._writer = writer

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
        try:
            arguments = json.loads(call.arguments)
        except json.JSONDecodeError as e:
            message = f"invalid JSON arguments: {e}"
            self._writer.append_tool_result(
                tool_call_id=call.id,
                content=message,
            )
            yield ToolExecutionFailed(
                request_id=tc.request_id,
                call=call,
                failure=ToolCallFailure(
                    error_kind=type(e).__name__,
                    message=message,
                ),
            )
            return

        yield ToolExecutionStarted(
            request_id=tc.request_id,
            call=call,
        )

        try:
            result = self._tools_service.execute(
                self._tool_ctx,
                DomainToolCall(tool_id=ToolId(call.name), arguments=arguments),
            )
        except ToolExecutionError as e:
            self._writer.append_tool_result(
                tool_call_id=call.id,
                content=e.message,
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

        self._writer.append_tool_result(
            tool_call_id=call.id,
            content=result.content,
        )
        yield ToolResultReady(
            request_id=tc.request_id,
            call=call,
            result=ToolCallResult(content=result.content),
        )


class RepeatedToolCallGuardMiddleware(StreamSource[AgentContext, AgentEvent]):
    """Подавляет (N+1)-й подряд идентичный ToolCallComplete."""

    def __init__(
        self,
        inner: StreamSource[AgentContext, AgentEvent],
        max_consecutive: int,
        writer: DialogueWriter,
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
            key = (call.name, call.arguments)
            if self._last == key:
                self._count += 1
            else:
                self._last = key
                self._count = 1

            if self._count > self._max_consecutive:
                message = (
                    f"Обнаружен луп: {self._count}-й подряд "
                    f"идентичный вызов '{call.name}' с "
                    f"arguments={call.arguments}. Результат уже "
                    f"есть в предыдущих role='tool' сообщениях. "
                    f"Либо вызови инструмент с другими аргументами, "
                    f"либо сформулируй ответ пользователю обычным "
                    f"текстом."
                )
                self._writer.append_tool_call_rejection(
                    tool_call_id=call.id,
                    content=message,
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


