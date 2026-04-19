"""Middleware-слои AgentLoop."""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator

from boba.domain.agent.events import (
    AgentEvent,
    GenerationDone,
    StageCompleted,
    StageStarted,
    ToolCallComplete,
    ToolResultReady,
    UserQueryReceived,
)
from boba.domain.agent.models import AgentConfig, AgentContext, AgentRequest, LLMMessage
from boba.domain.core.messages import MessageService
from boba.domain.core.patterns import Specification, Stream, StreamLoop
from boba.domain.core.promt import SystemPromptService, UserPromptService
from boba.domain.core.tools import (
    ToolCall,
    ToolId,
    ToolResult,
    ToolsService,
)

logger = logging.getLogger(__name__)


class Agent:
    def __init__(
        self,
        source: StreamLoop[AgentContext, None, AgentEvent],
        sink: Stream[AgentContext, AgentEvent, None],
    ) -> None:
        self._source = source
        self._sink = sink

    def name(self) -> str:
        return "AgentLoop"

    def run(self, config: AgentConfig, request: AgentRequest):
        """
        Запускает цикл обработки запроса агентом.
        """
        ctx = AgentContext(
            request=request,
            config=config,
        )

        for event in self._source.stream(ctx, None):
            for _ in self._sink.stream(ctx, event):
                pass


class SystemMessageMiddleware(Stream[AgentContext, None, AgentEvent]):
    """
    Добавляет system message на первой итерации,
    затем делегирует следующему слою.
    """

    def __init__(
        self,
        inner: Stream[AgentContext, None, AgentEvent],
        prompt_service: SystemPromptService,
        message_service: MessageService,
    ) -> None:
        self._inner = inner
        self._prompt_service = prompt_service
        self._message_service = message_service

    def name(self) -> str:
        return "SystemMessage"

    def stream(self, ctx: AgentContext, stream: None) -> Iterator[AgentEvent]:
        if self._message_service.last() is None:
            yield StageStarted(
                request_id=ctx.request.request_id,
                stage=self.name(),
            )

            system_prompt = self._prompt_service.build()
            self._message_service.add(
                LLMMessage(role="system", content=system_prompt.to_string()),
            )

            yield StageCompleted(
                request_id=ctx.request.request_id,
                stage=self.name(),
                detail="system prompt added",
            )

        yield from self._inner.stream(ctx, stream)


class UserMessageMiddleware(Stream[AgentContext, None, AgentEvent]):
    """
    Добавляет user message на первой итерации,
    затем делегирует следующему слою.
    """

    def __init__(
        self,
        inner: Stream[AgentContext, None, AgentEvent],
        user_prompt_service: UserPromptService,
        message_service: MessageService,
    ) -> None:
        self._inner = inner
        self._user_prompt_service = user_prompt_service
        self._message_service = message_service

    def name(self) -> str:
        return "UserMessage"

    def stream(self, ctx: AgentContext, stream: None) -> Iterator[AgentEvent]:
        if ctx.iteration == 1:
            yield StageStarted(request_id=ctx.request.request_id, stage=self.name())

            yield UserQueryReceived(
                request_id=ctx.request.request_id,
                query=ctx.request.query,
            )

            content = self._user_prompt_service.ctx(ctx).build().to_string()

            self._message_service.add(LLMMessage(role="user", content=content))

            yield StageCompleted(
                request_id=ctx.request.request_id,
                stage=self.name(),
                detail="user message added",
            )

        yield from self._inner.stream(ctx, stream)


class ToolExecutionMiddleware(Stream[AgentContext, None, AgentEvent]):
    """
    Выполняет tool calls, полученные от LLM.

    После того как внутренний стрим (LLM) заканчивает итерацию, собирает все
    ``ToolCallComplete`` события и по каждому:

    1. Парсит JSON ``arguments``. Битый JSON → ``ToolResult(is_error=True)``
       с понятным сообщением (LLM получит его обратно и сможет починить).
    2. Вызывает :meth:`ToolsService.execute`. Любые ошибки выполнения сам
       сервис завернёт в ``ToolResult(is_error=True)`` — исключения наружу
       не летят.
    3. Пишет ``LLMMessage(role="tool", tool_call_id=..., content=...)`` в
       :class:`MessageService` — на следующей итерации LLM увидит результат.
    4. Эмитит ``ToolResultReady`` в стрим событий — sink'ы его отрисуют/
       залогируют.

    Если LLM не запросила тулов — middleware просто проксирует события
    inner без побочных эффектов.
    """

    def __init__(
        self,
        inner: Stream[AgentContext, None, AgentEvent],
        tools_service: ToolsService,
        message_service: MessageService,
    ) -> None:
        self._inner = inner
        self._tools_service = tools_service
        self._message_service = message_service

    def name(self) -> str:
        return "ToolExecution"

    def stream(self, ctx: AgentContext, stream: None) -> Iterator[AgentEvent]:
        pending: list[ToolCallComplete] = []

        for event in self._inner.stream(ctx, stream):
            yield event
            if isinstance(event, ToolCallComplete):
                pending.append(event)

        for tc in pending:
            yield from self._run_tool(tc)

    def _run_tool(self, tc: ToolCallComplete) -> Iterator[AgentEvent]:
        try:
            arguments = json.loads(tc.arguments)
        except json.JSONDecodeError as e:
            result = ToolResult(
                content=f"invalid JSON arguments: {e}",
                is_error=True,
            )
        else:
            call = ToolCall(
                tool_id=ToolId(tc.tool_name),
                arguments=arguments,
            )
            result = self._tools_service.execute(None, call)

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
            is_error=result.is_error,
        )


class IterationCounterMiddleware(Stream[AgentContext, None, AgentEvent]):
    """
    Подсчет кол-ва итераций цикла агента.
    Увеличивает счетчик в контексте и делегирует следующему слою.
    """

    def __init__(self, inner: Stream[AgentContext, None, AgentEvent]) -> None:
        self._inner = inner

    def name(self) -> str:
        return "Counter"

    def stream(self, ctx: AgentContext, stream: None) -> Iterator[AgentEvent]:
        ctx.iteration += 1
        yield from self._inner.stream(ctx, stream)


class StopOnFinished(Specification[tuple[AgentContext, AgentEvent]]):
    """Останавливает если генерация завершена и не tool_calls."""

    def check(self, candidate: tuple[AgentContext, AgentEvent]) -> bool:
        _ctx, event = candidate

        if isinstance(event, GenerationDone):
            return event.finish_reason != "tool_calls"

        return False


class StopOnMaxIterations(Specification[tuple[AgentContext, AgentEvent]]):
    """
    Останавливает если превышен лимит итераций
    """

    def check(self, candidate: tuple[AgentContext, AgentEvent]) -> bool:
        ctx, event = candidate

        if isinstance(event, GenerationDone):
            return ctx.iteration >= ctx.config.max_iterations

        return False
