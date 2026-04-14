"""Стадии AgentLoop — каждая является StreamSource[AgentContext, AgentEvent]."""

from __future__ import annotations

import logging
from typing import Iterator

from boba.domain.agent.events import (
    AgentEvent,
    StageCompleted,
    StageStarted,
)
from boba.domain.agent.llm import LLMCompletionService
from boba.domain.agent.models import AgentContext, LLMMessage, LLMRequest
from boba.domain.core.messages import MessageService
from boba.domain.core.promt import SystemPromptService, UserPromptService
from boba.domain.core.stream import StreamSource

logger = logging.getLogger(__name__)


class SystemMessageStage(StreamSource[AgentContext, AgentEvent]):
    """
    Добавляет system message на первой итерации.
    На последующих — пропускает.
    """

    def __init__(
        self,
        prompt_service: SystemPromptService,
        message_service: MessageService,
    ) -> None:
        self._prompt_service = prompt_service
        self._message_service = message_service

    def name(self) -> str:
        return "SystemMessage"

    def produce(self, ctx: AgentContext) -> Iterator[AgentEvent]:
        if self._message_service.last() is not None:
            return

        yield StageStarted(stage=self.name())

        system_prompt = self._prompt_service.build()

        self._message_service.add(
            LLMMessage(
                role="system",
                content=system_prompt.build(),
            )
        )

        yield StageCompleted(stage=self.name(), detail="system prompt added")


class UserMessageStage(StreamSource[AgentContext, AgentEvent]):
    """
    Собирает user message через PromptService (user_prompt_service)
    и добавляет в MessageService. Выполняется один раз — на первой итерации.
    """

    def __init__(
        self,
        user_prompt_service: UserPromptService,
        message_service: MessageService,
    ) -> None:
        self._user_prompt_service = user_prompt_service
        self._message_service = message_service

    def name(self) -> str:
        return "UserMessage"

    def produce(self, ctx: AgentContext) -> Iterator[AgentEvent]:
        if ctx.iteration > 0:
            return

        yield StageStarted(stage=self.name())

        # Контекст от провайдеров (IDE selection, template и т.д.)
        context = self._user_prompt_service.build().build()

        # Собираем: контекст (если есть) + запрос пользователя
        parts = [p for p in (context, ctx.request.query) if p]
        content = "\n\n".join(parts)

        self._message_service.add(LLMMessage(role="user", content=content))

        yield StageCompleted(stage=self.name(), detail="user message added")


class GenerateStage(StreamSource[AgentContext, AgentEvent]):
    """
    Вызывает LLM (стриминг). Стримит ThinkingToken/AnswerToken.
    По завершении добавляет assistant message через MessageService.
    """

    def __init__(
        self,
        llm: LLMCompletionService,
        message_service: MessageService,
    ) -> None:
        self._llm = llm
        self._message_service = message_service

    def name(self) -> str:
        return "Generate"

    def produce(self, ctx: AgentContext) -> Iterator[AgentEvent]:
        ctx.iteration += 1
        yield StageStarted(stage=self.name())

        request = LLMRequest(
            model=ctx.request.model,
            messages=self._message_service.message_iter(),
        )

        yield from self._llm.produce(request)

        yield StageCompleted(stage=self.name(), detail="generation done")
