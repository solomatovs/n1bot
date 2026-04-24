from __future__ import annotations

import time
from collections.abc import Iterable, Sequence

from boba.domain.core.patterns import StreamSource
from boba_2.domain.agent.events import (
    AgentEvent,
    SystemPromptProcessed,
    SystemPromptProcessingStarted,
    UserPromptProcessed,
    UserPromptProcessingStarted,
    UserQueryReceived,
)
from boba_2.domain.agent.models import AgentContext
from boba_2.domain.agent.prompt import (
    PromptFactory,
    PromptKind,
    PromptProvider,
)


class SystemPromptMiddleware(StreamSource[AgentContext, AgentEvent]):
    """Собирает system-промпт и кладёт его в ``ctx.request.system_message``.

    Работает каждую итерацию — провайдеры могут отдавать разное
    содержимое в зависимости от текущего состояния (workspace, git,
    env, time). :class:`~boba_2.domain.llm.models.LLMRequest`
    пересоздаётся на каждой итерации, так что system-слот всегда
    содержит только свежую сборку.

    Пустой результат сборки → ``system_message`` остаётся ``None``.
    """

    def __init__(
        self,
        inner: StreamSource[AgentContext, AgentEvent],
        prompt_providers: Sequence[PromptProvider],
    ) -> None:
        self._inner = inner
        self._prompt_providers = prompt_providers

    def name(self) -> str:
        return "SystemPrompt"

    def reset(self) -> None:
        self._inner.reset()

    def stream(self, ctx: AgentContext) -> Iterable[AgentEvent]:
        rid = ctx.agent_request.request_id
        yield SystemPromptProcessingStarted(request_id=rid)

        start = time.perf_counter()
        content = (
            PromptFactory(ctx, self._prompt_providers)
            .build()
            .to_string(PromptKind.SYSTEM)
        )
        duration_ms = (time.perf_counter() - start) * 1000

        if content:
            ctx.request.set_system_message(content)

        yield SystemPromptProcessed(
            request_id=rid,
            content=content,
            duration_ms=duration_ms,
        )

        yield from self._inner.stream(ctx)


class UserPromptMiddleware(StreamSource[AgentContext, AgentEvent]):
    def __init__(
        self,
        inner: StreamSource[AgentContext, AgentEvent],
        prompt_providers: Sequence[PromptProvider],
    ) -> None:
        self._inner = inner
        self._prompt_providers = prompt_providers

    def name(self) -> str:
        return "UserPrompt"

    def reset(self) -> None:
        self._inner.reset()

    def stream(self, ctx: AgentContext) -> Iterable[AgentEvent]:
        rid = ctx.agent_request.request_id
        yield UserPromptProcessingStarted(request_id=rid)

        start = time.perf_counter()
        content = (
            PromptFactory(ctx, self._prompt_providers)
            .build()
            .to_string(PromptKind.USER)
        )
        duration_ms = (time.perf_counter() - start) * 1000

        if content:
            ctx.request.set_user_message(content)
            if ctx.iteration == 1:
                yield UserQueryReceived(request_id=rid, query=content)

        yield UserPromptProcessed(
            request_id=rid,
            content=content,
            duration_ms=duration_ms,
        )

        yield from self._inner.stream(ctx)
