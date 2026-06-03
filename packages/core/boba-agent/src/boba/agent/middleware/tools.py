"""Middleware для tools: исполнение tool_calls из итогового TotalMessage."""

from __future__ import annotations

from collections.abc import Iterable

from boba.agent.agent import AgentContext
from boba.agent.events import (
    AgentEvent,
    ToolExecutionFailed,
    ToolExecutionStarted,
    ToolResultReady,
    TotalMessage,
)
from boba.agent.models import ToolCallFailure, ToolCallResult
from boba.llm.models import RequestId, ToolCall
from boba.patterns import StreamSource
from boba.tools.domain import ToolCall as DomainToolCall
from boba.tools.domain import (
    ToolContext,
    ToolExecutionError,
    ToolId,
)
from boba.tools.framework import ToolExecutor


class ToolExecutionMiddleware(StreamSource[AgentContext, AgentEvent]):
    """Исполняет tool_calls из итогового TotalMessage после inner-стрима.

    Контракт: источник истины для исполнения — `TotalMessage.message.tool_calls`
    (итоговые поля завершённой генерации), а не потоковые `ToolCallMessage`.
    Последние — наблюдательные (UI/рендер) и на исполнение не влияют.
    """

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
        for event in self._inner.stream(ctx):
            yield event

            # Выполняем tool_calls только после завершения всего вывода от llm
            # Итоговое сообщение это TotalMessage с полным списком tool_calls,
            # Так приходиться делать, потому что в процессе прихода чанков
            # нам может поступить некорректные данные, например tool_call в поле content
            # а провайдер занимается эвристикой и пытается перемапить в корректное сообщ
            # и TotalMessage отправляет уже корректным
            if isinstance(event, TotalMessage):
                for call in event.message.tool_calls:
                    yield from self._run_tool(event.request_id, call)

    def _run_tool(
        self,
        request_id: RequestId,
        call: ToolCall,
    ) -> Iterable[AgentEvent]:
        yield ToolExecutionStarted(
            request_id=request_id,
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
                request_id=request_id,
                call=call,
                result=ToolCallResult(result=result),
            )
        except ToolExecutionError as e:
            yield ToolExecutionFailed(
                request_id=request_id,
                call=call,
                failure=ToolCallFailure(
                    error_kind=type(e).__name__,
                    message=e.message,
                ),
            )
