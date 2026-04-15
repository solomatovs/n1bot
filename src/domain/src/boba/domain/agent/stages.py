"""Middleware-слои AgentLoop."""

from __future__ import annotations

import logging
from typing import Iterator

from boba.domain.agent.events import (
    AgentEvent,
    StageCompleted,
    StageStarted,
    UserQueryReceived,
)
from boba.domain.agent.llm import LLMMiddleware
from boba.domain.agent.models import AgentContext, LLMMessage
from boba.domain.core.messages import MessageService
from boba.domain.core.promt import SystemPromptService, UserPromptService

logger = logging.getLogger(__name__)


class SystemMessageMiddleware(LLMMiddleware):
    """
    Добавляет system message на первой итерации,
    затем делегирует следующему слою.
    """

    def __init__(
        self,
        next: LLMMiddleware,
        prompt_service: SystemPromptService,
        message_service: MessageService,
    ) -> None:
        self._next = next
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

        yield from self._next.produce(ctx)


class UserMessageMiddleware(LLMMiddleware):
    """
    Добавляет user message на первой итерации,
    затем делегирует следующему слою.
    """

    def __init__(
        self,
        next: LLMMiddleware,
        user_prompt_service: UserPromptService,
        message_service: MessageService,
    ) -> None:
        self._next = next
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

        yield from self._next.produce(ctx)


class IterationCounterMiddleware(LLMMiddleware):
    """
    Подсчет кол-ва итераций цикла агента.
    Увеличивает счетчик в контексте и делегирует следующему слою.
    """

    def __init__(self, next: LLMMiddleware) -> None:
        self._next = next

    def name(self) -> str:
        return "Counter"

    def produce(self, ctx: AgentContext) -> Iterator[AgentEvent]:
        ctx.iteration += 1
        yield from self._next.produce(ctx)
