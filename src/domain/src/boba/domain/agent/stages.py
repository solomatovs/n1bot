"""Middleware-слои AgentLoop."""

from __future__ import annotations

import logging
from collections.abc import Iterator

from boba.domain.agent.events import (
    AgentEvent,
    StageCompleted,
    StageStarted,
    UserQueryReceived,
)
from boba.domain.agent.loop import AgentMiddleware
from boba.domain.agent.models import AgentContext, LLMMessage
from boba.domain.core.messages import MessageService
from boba.domain.core.promt import SystemPromptService, UserPromptService

logger = logging.getLogger(__name__)


class SystemMessageMiddleware(AgentMiddleware):
    """
    Добавляет system message на первой итерации,
    затем делегирует следующему слою.
    """

    def __init__(
        self,
        inner: AgentMiddleware,
        prompt_service: SystemPromptService,
        message_service: MessageService,
    ) -> None:
        self._inner = inner
        self._prompt_service = prompt_service
        self._message_service = message_service

    def name(self) -> str:
        return "SystemMessage"

    def produce(self, ctx: AgentContext) -> Iterator[AgentEvent]:
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

        yield from self._inner.produce(ctx)


class UserMessageMiddleware(AgentMiddleware):
    """
    Добавляет user message на первой итерации,
    затем делегирует следующему слою.
    """

    def __init__(
        self,
        inner: AgentMiddleware,
        user_prompt_service: UserPromptService,
        message_service: MessageService,
    ) -> None:
        self._inner = inner
        self._user_prompt_service = user_prompt_service
        self._message_service = message_service

    def name(self) -> str:
        return "UserMessage"

    def produce(self, ctx: AgentContext) -> Iterator[AgentEvent]:
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

        yield from self._inner.produce(ctx)


class IterationCounterMiddleware(AgentMiddleware):
    """
    Подсчет кол-ва итераций цикла агента.
    Увеличивает счетчик в контексте и делегирует следующему слою.
    """

    def __init__(self, inner: AgentMiddleware) -> None:
        self._inner = inner

    def name(self) -> str:
        return "Counter"

    def produce(self, ctx: AgentContext) -> Iterator[AgentEvent]:
        ctx.iteration += 1
        yield from self._inner.produce(ctx)
