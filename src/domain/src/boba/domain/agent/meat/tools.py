"""
Middleware для tools: исполнение + защита от лупов.

Каталог tools собирается в :class:`TurnSpec` через
:class:`~boba.domain.agent.turn.reducers.ToolsReducer` — отдельного
``ToolsDefinitionMiddleware`` больше нет.
"""

from __future__ import annotations

import json
from collections.abc import Iterable

from boba.domain.agent.dialogue_writer import DialogueWriter
from boba.domain.agent.errors import RepeatedFormatFailureError
from boba.domain.agent.events import (
    AgentEvent,
    ToolCallComplete,
    ToolCallFormatFailed,
    ToolExecutionFailed,
    ToolExecutionStarted,
    ToolResultReady,
)
from boba.domain.agent.meat.error_routing import AgentErrorRouter
from boba.domain.agent.models import AgentContext
from boba.domain.core.patterns import StreamSource
from boba.domain.core.tools import (
    ToolCall,
    ToolContext,
    ToolExecutionError,
    ToolId,
    ToolsService,
)
from boba.domain.llm.models import LLMMessage


class ToolExecutionMiddleware(StreamSource[AgentContext, AgentEvent]):
    """
    Исполняет tool_calls после завершения inner-стрима.

    На каждый :class:`ToolCallComplete` пишет результат в историю
    через :class:`DialogueWriter` — и успех, и ошибка идут одним
    путём ``role="tool"`` в следующий виток. Ошибки параллельно
    yield'ят :class:`ToolExecutionFailed` для sink'ов.
    """

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
        try:
            arguments = json.loads(tc.arguments)
        except json.JSONDecodeError as e:
            self._writer.append_tool_result(
                tool_call_id=tc.tool_call_id,
                content=f"invalid JSON arguments: {e}",
            )
            yield ToolExecutionFailed(
                request_id=tc.request_id,
                tool_call_id=tc.tool_call_id,
                tool_name=tc.tool_name,
                error_kind=type(e).__name__,
                message=f"invalid JSON arguments: {e}",
            )
            return

        yield ToolExecutionStarted(
            request_id=tc.request_id,
            tool_call_id=tc.tool_call_id,
            tool_name=tc.tool_name,
        )

        try:
            result = self._tools_service.execute(
                self._tool_ctx,
                ToolCall(tool_id=ToolId(tc.tool_name), arguments=arguments),
            )
        except ToolExecutionError as e:
            self._writer.append_tool_result(
                tool_call_id=tc.tool_call_id,
                content=e.message,
            )
            yield ToolExecutionFailed(
                request_id=tc.request_id,
                tool_call_id=tc.tool_call_id,
                tool_name=tc.tool_name,
                error_kind=type(e).__name__,
                message=e.message,
            )
            return

        self._writer.append_tool_result(
            tool_call_id=tc.tool_call_id,
            content=result.content,
        )
        yield ToolResultReady(
            request_id=tc.request_id,
            tool_call_id=tc.tool_call_id,
            tool_name=tc.tool_name,
            content=result.content,
        )


class RepeatedToolCallGuardMiddleware(StreamSource[AgentContext, AgentEvent]):
    """
    Подавляет ``(N+1)``-й подряд идентичный :class:`ToolCallComplete`.

    На подавлении пишет в историю через :class:`DialogueWriter`
    feedback с ``role="tool"`` — LLM на следующей итерации увидит
    замечание вместо дублирующего вызова.
    """

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

            key = (event.tool_name, event.arguments)
            if self._last == key:
                self._count += 1
            else:
                self._last = key
                self._count = 1

            if self._count > self._max_consecutive:
                message = (
                    f"Обнаружен луп: {self._count}-й подряд "
                    f"идентичный вызов '{event.tool_name}' с "
                    f"arguments={event.arguments}. Результат уже "
                    f"есть в предыдущих role='tool' сообщениях. "
                    f"Либо вызови инструмент с другими аргументами, "
                    f"либо сформулируй ответ пользователю обычным "
                    f"текстом."
                )
                self._writer.append_llm_feedback(
                    LLMMessage(
                        role="tool",
                        content=message,
                        tool_call_id=event.tool_call_id,
                    ),
                )
                yield ToolExecutionFailed(
                    request_id=event.request_id,
                    tool_call_id=event.tool_call_id,
                    tool_name=event.tool_name,
                    error_kind="RepeatedToolCallError",
                    message=message,
                )
                continue

            yield event


class RepeatedFormatFailureGuardMiddleware(StreamSource[AgentContext, AgentEvent]):
    """
    Защита от залипания модели на неверном формате tool call.

    После N+1 подряд :class:`ToolCallFormatFailed` без
    :class:`ToolResultReady` между ними — поднимает
    :class:`RepeatedFormatFailureError` (terminal), роутер останавливает цикл.
    """

    def __init__(
        self,
        inner: StreamSource[AgentContext, AgentEvent],
        error_router: AgentErrorRouter,
        max_consecutive: int,
    ) -> None:
        self._inner = inner
        self._router = error_router
        self._max_consecutive = max_consecutive
        self._count = 0

    def name(self) -> str:
        return "RepeatedFormatFailureGuard"

    def reset(self) -> None:
        self._count = 0
        self._inner.reset()

    def stream(self, ctx: AgentContext) -> Iterable[AgentEvent]:
        for event in self._inner.stream(ctx):
            yield event

            if isinstance(event, ToolCallFormatFailed):
                self._count += 1
                if self._count > self._max_consecutive:
                    yield from self._router.route(
                        ctx,
                        RepeatedFormatFailureError(
                            f"Модель {self._count} раз подряд вывела неверный "
                            f"формат tool call (лимит {self._max_consecutive}). "
                            f"Дальнейшие объяснения формата не помогают — "
                            f"цикл остановлен.",
                            count=self._count,
                            limit=self._max_consecutive,
                        ),
                    )
                    return
            elif isinstance(event, ToolResultReady):
                self._count = 0
