"""Middleware для tools: определение каталога, исполнение вызовов,
защита от лупа идентичных вызовов."""

from __future__ import annotations

import json
from collections.abc import Iterator

from boba.domain.agent.errors import ToolFeedbackError
from boba.domain.agent.events import (
    AgentEvent,
    ToolCallComplete,
    ToolResultReady,
)
from boba.domain.agent.messages import MessageService
from boba.domain.agent.models import AgentContext, LLMMessage
from boba.domain.core.patterns import StreamSource
from boba.domain.core.tools import ToolCall, ToolExecutionError, ToolId, ToolsService

from .error_routing import AgentErrorRouter


class ToolsDefinitionMiddleware(StreamSource[AgentContext, AgentEvent]):
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
        inner: StreamSource[AgentContext, AgentEvent],
        tools_service: ToolsService,
    ) -> None:
        self._inner = inner
        self._tools_service = tools_service

    def name(self) -> str:
        return "ToolsDefinition"

    def stream(self, ctx: AgentContext) -> Iterator[AgentEvent]:
        ctx.llm_builder.tools = list(self._tools_service.tools())
        yield from self._inner.stream(ctx)


class ToolExecutionMiddleware(StreamSource[AgentContext, AgentEvent]):
    """
    Выполняет tool calls, полученные от LLM.

    После того как внутренний стрим (LLM) заканчивает итерацию, собирает все
    ``ToolCallComplete`` события и по каждому:

    1. Парсит JSON ``arguments``. Битый JSON → :class:`ToolFeedbackError`
       (LLM увидит ошибку и сможет починить).
    2. Вызывает :meth:`ToolsService.execute`. Сервис бросает «сырую»
       :class:`ToolExecutionError` (без знания про tool_call_id) — тут
       обогащается идентификатором вызова и пробрасывается как
       :class:`ToolFeedbackError`.
    3. Успех → ``LLMMessage(role="tool", tool_call_id=..., content=...)`` +
       :class:`ToolResultReady`.

    Batch-семантика делегирована :meth:`AgentErrorRouter.run_batch`:
    успешные события стримятся сразу, :class:`LLMFeedbackError` из
    любой подзадачи копится и маршрутизируется в конце — так одна
    упавшая подзадача не обрывает остальные. Терминальные ошибки
    пропускаются наверх во внешний :class:`AgentErrorRouterMiddleware`.

    Если LLM не запросила тулов — middleware просто проксирует события
    inner без побочных эффектов.
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

    def stream(self, ctx: AgentContext) -> Iterator[AgentEvent]:
        pending: list[ToolCallComplete] = []

        for event in self._inner.stream(ctx):
            yield event
            if isinstance(event, ToolCallComplete):
                pending.append(event)

        yield from self._error_router.run_batch(
            ctx, (self._run_tool(tc) for tc in pending)
        )

    def _run_tool(self, tc: ToolCallComplete) -> Iterator[AgentEvent]:
        try:
            arguments = json.loads(tc.arguments)
            call = ToolCall(
                tool_id=ToolId(tc.tool_name),
                arguments=arguments,
            )
            result = self._tools_service.execute(None, call)
        except json.JSONDecodeError as e:
            raise ToolFeedbackError(
                tool_call_id=tc.tool_call_id,
                tool_name=tc.tool_name,
                error_kind=type(e).__name__,
                message=f"invalid JSON arguments: {e}",
            ) from e
        except ToolExecutionError as e:
            raise ToolFeedbackError(
                tool_call_id=tc.tool_call_id,
                tool_name=tc.tool_name,
                error_kind=type(e).__name__,
                message=e.message,
            ) from e

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
    """Защита от лупа: подавляет ``(N+1)``-й идентичный
    :class:`ToolCallComplete` подряд и маршрутизирует его как
    :class:`ToolFeedbackError`.

    На следующей итерации LLM увидит текст ошибки в ``role="tool"``
    сообщении (роутер пишет сам через :class:`MessageService`) и должен
    сменить тактику — либо позвать инструмент с другими аргументами,
    либо ответить пользователю обычным текстом.

    Сидит **внутри** :class:`ToolExecutionMiddleware`. Подавлённый
    ``ToolCallComplete`` не долетает до батча выполнения — реальный
    tool не дёргается. Запись ``LLMMessage(role="tool")`` и эмит
    :class:`ToolExecutionFailed` делегированы
    :class:`AgentErrorRouter.route` — той же машинерии, что использует
    сам :class:`ToolExecutionMiddleware` для штатных
    :class:`ToolFeedbackError`.

    Сравнение «идентичный» — exact match по ``(tool_name, arguments)``,
    где ``arguments`` это сырая JSON-строка из события (так же, как её
    видит downstream). JSON-нормализация (порядок ключей, пробелы) пока
    не делается — простота важнее.

    Состояние (последний вызов + счётчик подряд) живёт на инстансе
    middleware. ``agent_chain`` собирается per-request (scope=REQUEST),
    инстанс — fresh на каждый запрос, отдельный сброс не нужен.
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

    def stream(self, ctx: AgentContext) -> Iterator[AgentEvent]:
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
