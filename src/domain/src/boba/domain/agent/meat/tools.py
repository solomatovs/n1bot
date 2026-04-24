"""
Middleware для tools: каталог + исполнение
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator

from boba.domain.agent.errors import (
    RepeatedFormatFailureError,
    ToolFeedbackError,
)
from boba.domain.agent.events import (
    AgentEvent,
    ToolCallComplete,
    ToolCallFormatFailed,
    ToolExecutionStarted,
    ToolResultReady,
)
from boba.domain.agent.meat.error_routing import AgentErrorRouter
from boba.domain.agent.messages import MessageService
from boba.domain.agent.models import AgentContext
from boba.domain.core.patterns import StreamSource
from boba.domain.core.tools import (
    ToolCall,
    ToolExecutionError,
    ToolId,
    ToolsService,
)
from boba.domain.llm.models import LLMMessage


class ToolsDefinitionMiddleware(StreamSource[AgentContext, AgentEvent]):
    """
    Кладёт каталог tools в ``ctx.request.tools`` на каждой итерации.
    """

    def __init__(
        self,
        inner: StreamSource[AgentContext, AgentEvent],
        tools_service: ToolsService,
    ) -> None:
        self._inner = inner
        self._tools_service = tools_service

    def name(self) -> str:
        return "ToolsDefinition"

    def reset(self) -> None:
        self._inner.reset()

    def stream(self, ctx: AgentContext) -> Iterable[AgentEvent]:
        ctx.request.set_tools(self._tools_service.tools())
        ctx.request.parallel_tool_calls = True
        yield from self._inner.stream(ctx)


class ToolExecutionMiddleware(StreamSource[AgentContext, AgentEvent]):
    """
    Исполняет tool_calls после завершения inner-стрима.
    """

    def __init__(
        self,
        inner: StreamSource[AgentContext, AgentEvent],
        tools_service: ToolsService,
        message_service: MessageService,
        error_router: AgentErrorRouter,
    ) -> None:
        self._inner = inner
        self._tools_service = tools_service
        self._message_service = message_service
        self._error_router = error_router

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

        yield from self._error_router.run_batch(
            ctx,
            (self._run_tool(tc) for tc in pending),
        )

    def _run_tool(self, tc: ToolCallComplete) -> Iterator[AgentEvent]:
        """Одна подзадача: parse → execute → result.

        Ошибки оборачиваются в :class:`ToolFeedbackError` — роутер
        превратит в ``role="tool"`` сообщение + событие для sink'ов.
        """
        # JSON-decode в отдельной ветке — если args невалидны, tool
        # не начинал исполняться: ToolExecutionStarted эмитить нечестно.
        try:
            arguments = json.loads(tc.arguments)
        except json.JSONDecodeError as e:
            raise ToolFeedbackError(
                tool_call_id=tc.tool_call_id,
                tool_name=tc.tool_name,
                error_kind=type(e).__name__,
                message=f"invalid JSON arguments: {e}",
            ) from e

        call = ToolCall(
            tool_id=ToolId(tc.tool_name),
            arguments=arguments,
        )

        yield ToolExecutionStarted(
            request_id=tc.request_id,
            tool_call_id=tc.tool_call_id,
            tool_name=tc.tool_name,
        )

        try:
            result = self._tools_service.execute(None, call)
        except ToolExecutionError as e:
            raise ToolFeedbackError(
                tool_call_id=tc.tool_call_id,
                tool_name=tc.tool_name,
                error_kind=type(e).__name__,
                message=e.message,
            ) from e

        # Успех — фидбек для LLM пишем здесь (role="tool" с результатом).
        # В роутер это не идёт: роутер обслуживает ОШИБКИ, для
        # успешного результата отдельная ``to_llm_feedback``-ветка была
        # бы семантическим натяжением. Источник истины для tool-feedback
        # — MessageService, middleware обращается к нему прямо.
        self._message_service.add(
            LLMMessage(
                role="tool",
                content=result.content,
                tool_call_id=tc.tool_call_id,
            ),
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
                yield from self._router.route(
                    ctx,
                    ToolFeedbackError(
                        tool_call_id=event.tool_call_id,
                        tool_name=event.tool_name,
                        error_kind="RepeatedToolCallError",
                        message=(
                            f"Обнаружен луп: {self._count}-й подряд "
                            f"идентичный вызов '{event.tool_name}' с "
                            f"arguments={event.arguments}. Результат уже "
                            f"есть в предыдущих role='tool' сообщениях. "
                            f"Либо вызови инструмент с другими аргументами, "
                            f"либо сформулируй ответ пользователю обычным "
                            f"текстом."
                        ),
                    ),
                )
                continue

            yield event


class RepeatedFormatFailureGuardMiddleware(StreamSource[AgentContext, AgentEvent]):
    """
    Защита от залипания модели на неверном формате tool call.
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
