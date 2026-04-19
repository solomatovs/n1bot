"""Middleware-слои AgentLoop."""

from __future__ import annotations

import logging
from collections.abc import Iterator

from boba.domain.agent.events import (
    AgentEvent,
    GenerationDone,
    StageCompleted,
    StageStarted,
    UserQueryReceived,
)
from boba.domain.agent.models import AgentConfig, AgentContext, AgentRequest, LLMMessage
from boba.domain.core.messages import MessageService
from boba.domain.core.patterns import Specification, Stream, StreamLoop
from boba.domain.core.promt import SystemPromptService, UserPromptService

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

            system_prompt = self._prompt_service.build(None)
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

            content = self._user_prompt_service.build(ctx).to_string()

            self._message_service.add(LLMMessage(role="user", content=content))

            yield StageCompleted(
                request_id=ctx.request.request_id,
                stage=self.name(),
                detail="user message added",
            )

        yield from self._inner.stream(ctx, stream)


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
