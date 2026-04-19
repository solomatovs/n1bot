"""Middleware сборки system/user промптов."""

from __future__ import annotations

from collections.abc import Iterator

from boba.domain.agent.events import (
    AgentEvent,
    StageCompleted,
    StageStarted,
    UserQueryReceived,
)
from boba.domain.agent.messages import MessageService
from boba.domain.agent.models import AgentContext, LLMMessage
from boba.domain.agent.prompt import PromptFactory, PromptKind, PromptProvider
from boba.domain.core.patterns import StreamSource


class SystemPromptMiddleware(StreamSource[AgentContext, AgentEvent]):
    """Строит system-prompt через :class:`PromptFactory` (срез
    ``PromptKind.SYSTEM``) и кладёт его в ``ctx.llm_builder.system_prompt``.
    :class:`LLMRequestFactory` читает этот слот при сборке :class:`LLMRequest`.
    Отключение middleware через DI убирает system-prompt из запроса.
    """

    def __init__(
        self,
        inner: StreamSource[AgentContext, AgentEvent],
        prompt_providers: list[PromptProvider],
    ) -> None:
        self._inner = inner
        self._prompt_providers = prompt_providers

    def name(self) -> str:
        return "SystemPrompt"

    def stream(self, ctx: AgentContext) -> Iterator[AgentEvent]:
        content = (
            PromptFactory(ctx, self._prompt_providers)
            .build()
            .to_string(PromptKind.SYSTEM)
        )
        if content:
            ctx.llm_builder.system_prompt = content

        yield from self._inner.stream(ctx)


class UserPromptMiddleware(StreamSource[AgentContext, AgentEvent]):
    """На первой итерации строит user-prompt через :class:`PromptFactory`
    (срез ``PromptKind.USER``) и добавляет его как
    :class:`LLMMessage` с ``role="user"`` в :class:`MessageService`.
    Эмитит :class:`UserQueryReceived` для sink'ов.

    User-prompt — часть диалога (append-once), а не пересчитываемый
    слот. Его правильное место — в :class:`MessageService`, где он
    становится в конец истории (после replay'а предыдущих сессий) и
    дальше не меняется. :class:`LLMRequestFactory` читает
    :class:`MessageService` и не знает, кто его туда положил.
    """

    def __init__(
        self,
        inner: StreamSource[AgentContext, AgentEvent],
        prompt_providers: list[PromptProvider],
        message_service: MessageService,
    ) -> None:
        self._inner = inner
        self._prompt_providers = prompt_providers
        self._message_service = message_service

    def name(self) -> str:
        return "UserPrompt"

    def stream(self, ctx: AgentContext) -> Iterator[AgentEvent]:
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
                self._message_service.add(LLMMessage(role="user", content=content))

            yield StageCompleted(
                request_id=ctx.request.request_id,
                stage=self.name(),
                detail="user prompt added",
            )

        yield from self._inner.stream(ctx)
