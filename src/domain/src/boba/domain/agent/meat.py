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
from boba.domain.core.promt import PromptFactory, PromptKind, PromptProvider
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


class SystemPromptMiddleware(Stream[AgentContext, None, AgentEvent]):
    """Строит system-prompt через :class:`PromptFactory` (срез
    ``PromptKind.SYSTEM``) и кладёт его в ``ctx.llm_builder.system_prompt``.
    :class:`LLMRequestFactory` читает этот слот при сборке :class:`LLMRequest`.
    Отключение middleware через DI убирает system-prompt из запроса.
    """

    def __init__(
        self,
        inner: Stream[AgentContext, None, AgentEvent],
        prompt_providers: list[PromptProvider],
    ) -> None:
        self._inner = inner
        self._prompt_providers = prompt_providers

    def name(self) -> str:
        return "SystemPrompt"

    def stream(self, ctx: AgentContext, stream: None) -> Iterator[AgentEvent]:
        content = (
            PromptFactory(ctx, self._prompt_providers)
            .build()
            .to_string(PromptKind.SYSTEM)
        )
        if content:
            ctx.llm_builder.system_prompt = content

        yield from self._inner.stream(ctx, stream)


class UserPromptMiddleware(Stream[AgentContext, None, AgentEvent]):
    """Строит user-prompt через :class:`PromptFactory` (срез
    ``PromptKind.USER``) и кладёт его в ``ctx.llm_builder.user_prompt``.
    На первой итерации эмитит :class:`UserQueryReceived` для sink'ов.

    User-prompt не хранится в :class:`MessageService` — он пересобирается
    каждую итерацию из идемпотентных provider'ов. :class:`LLMRequestFactory`
    читает слот и prepend'ит его перед снимком диалога.
    """

    def __init__(
        self,
        inner: Stream[AgentContext, None, AgentEvent],
        prompt_providers: list[PromptProvider],
    ) -> None:
        self._inner = inner
        self._prompt_providers = prompt_providers

    def name(self) -> str:
        return "UserPrompt"

    def stream(self, ctx: AgentContext, stream: None) -> Iterator[AgentEvent]:
        if ctx.iteration == 1:
            yield StageStarted(request_id=ctx.request.request_id, stage=self.name())
            yield UserQueryReceived(
                request_id=ctx.request.request_id,
                query=ctx.request.query,
            )

        content = (
            PromptFactory(ctx, self._prompt_providers)
            .build()
            .to_string(PromptKind.USER)
        )
        if content:
            ctx.llm_builder.user_prompt = content

        if ctx.iteration == 1:
            yield StageCompleted(
                request_id=ctx.request.request_id,
                stage=self.name(),
                detail="user prompt built",
            )

        yield from self._inner.stream(ctx, stream)


class ToolsDefinitionMiddleware(Stream[AgentContext, None, AgentEvent]):
    """Кладёт текущий снимок каталога :class:`ToolsService` в
    ``ctx.llm_builder.tools``. :class:`LLMRequestFactory` читает этот слот
    при сборке :class:`LLMRequest` и мапит в ``kwargs["tools"]`` провайдера.

    Отключение middleware через DI — запрос уходит без tools, LLM не видит
    инструментов и не вызывает их. Плагины могут зарегистрировать свою
    реализацию (фильтрация по ролям, лимит по количеству, динамический
    ``tool_choice``) без правки фабрики.
    """

    def __init__(
        self,
        inner: Stream[AgentContext, None, AgentEvent],
        tools_service: ToolsService,
    ) -> None:
        self._inner = inner
        self._tools_service = tools_service

    def name(self) -> str:
        return "ToolsDefinition"

    def stream(self, ctx: AgentContext, stream: None) -> Iterator[AgentEvent]:
        ctx.llm_builder.tools = list(self._tools_service.tools())
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
