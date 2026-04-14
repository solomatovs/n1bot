"""Стадии AgentLoop — каждая является StreamSource[AgentContext, AgentEvent]."""

from __future__ import annotations

import logging
from typing import Iterator

from boba.domain.agent.events import (
    AgentEvent,
    AnswerToken,
    GenerationDone,
    StageCompleted,
    StageStarted,
    ThinkingToken,
)
from boba.domain.agent.models import AgentContext
from boba.domain.llm.llm import LLMCompletionService, LLMMessage, LLMRequest
from boba.domain.core.messages import MessageService
from boba.domain.core.promt import SystemPromptService
from boba.domain.core.stream import StreamSource

logger = logging.getLogger(__name__)


class BuildMessagesStage(StreamSource[AgentContext, AgentEvent]):
    """
    Первая стадия: на первой итерации формирует system + user message
    через MessageService. На последующих — пропускает.
    """

    def __init__(
        self,
        prompt_service: SystemPromptService,
        message_service: MessageService,
    ) -> None:
        self._prompt_service = prompt_service
        self._message_service = message_service

    def name(self) -> str:
        return "BuildMessages"

    def produce(self, ctx: AgentContext) -> Iterator[AgentEvent]:
        if self._message_service.last() is not None:
            return

        yield StageStarted(stage=self.name())

        system_prompt = self._prompt_service.build().build()
        self._message_service.add(LLMMessage(role="system", content=system_prompt))
        self._message_service.add(LLMMessage(role="user", content=ctx.request.query))

        yield StageCompleted(stage=self.name(), detail="messages initialized")


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

        for delta in self._llm.produce(request):
            if delta.thinking:
                yield ThinkingToken(token=delta.thinking)
            if delta.content:
                yield AnswerToken(token=delta.content)

        yield GenerationDone()
        yield StageCompleted(
            stage=self.name(),
            detail="tokens=1",
        )
