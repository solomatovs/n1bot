"""Middleware для первой итерации: перевод сырого запроса в эффект.

:class:`InitialUserQueryMiddleware` собирает USER-блок через
:class:`PromptFactory` и декларирует :class:`UserQueryEffect`; дальше
trigger консьюмит :class:`LLMInvokeMiddleware` (terminal цепочки),
применяет эффекты к :class:`MessageService` через :meth:`TurnSpec.initial`
и собирает :class:`LLMRequest`.

Порядок в цепочке::

    IterationCounter
      → InitialUserQuery    (декларирует эффект на iter==1)
      → AssistantPersist
      → ToolExecution
      → RepeatedToolGuard
      → StrictJson
      → LLMInvoke (terminal: consume trigger + build request + invoke)
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from boba.domain.agent.events import AgentEvent, UserQueryReceived
from boba.domain.agent.models import AgentContext
from boba.domain.agent.prompt import PromptFactory, PromptKind, PromptProvider
from boba.domain.agent.turn.effects import UserQueryEffect
from boba.domain.core.patterns import StreamSource


class InitialUserQueryMiddleware(StreamSource[AgentContext, AgentEvent]):
    """На первой итерации декларирует :class:`UserQueryEffect`.

    Собирает USER-блок через существующую :class:`PromptFactory`
    (переиспользуя всех :class:`PromptProvider` kind=USER). Эмитит
    :class:`UserQueryReceived` один раз для sink'ов.

    На последующих итерациях middleware — no-op: user-prompt не
    пересобирается, единственное пользовательское сообщение остаётся
    приколоченным к истории. Последующие witki инициируются
    producer'ами (:class:`ToolResultEffect` / :class:`LLMFeedbackEffect`).
    """

    def __init__(
        self,
        inner: StreamSource[AgentContext, AgentEvent],
        prompt_providers: Sequence[PromptProvider],
    ) -> None:
        self._inner = inner
        self._prompt_providers = prompt_providers

    def name(self) -> str:
        return "InitialUserQuery"

    def reset(self) -> None:
        self._inner.reset()

    def stream(self, ctx: AgentContext) -> Iterable[AgentEvent]:
        if ctx.iteration == 1:
            content = (
                PromptFactory(ctx, self._prompt_providers)
                .build()
                .to_string(PromptKind.USER)
            )
            if content:
                ctx.triggers.declare(UserQueryEffect(content=content), "user_query")
                yield UserQueryReceived(
                    request_id=ctx.agent_request.request_id,
                    query=content,
                )
        yield from self._inner.stream(ctx)
