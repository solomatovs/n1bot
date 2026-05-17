"""Полиморфная маршрутизация RoutableError по маркерам."""

from __future__ import annotations

from collections.abc import Iterable, Iterator

from boba.agent.agent import AgentContext
from boba.agent.errors import AgentLLMFeedbackError, RoutableError, UserFeedbackError
from boba.agent.events import AgentEvent, FeedbackToLLMAdded
from boba.llm.models import RequestId
from boba.patterns import StreamSource


class AgentErrorRouter:
    """Маршрутизирует RoutableError по маркерам."""

    def route(
        self,
        request_id: RequestId,
        err: RoutableError,
    ) -> Iterator[AgentEvent]:
        if isinstance(err, AgentLLMFeedbackError):
            feedback = err.to_llm_feedback()
            yield FeedbackToLLMAdded(
                request_id=request_id,
                content=feedback.content,
            )

        if isinstance(err, UserFeedbackError):
            yield err.to_user_feedback(request_id)


class AgentErrorRouterMiddleware(StreamSource[AgentContext, AgentEvent]):
    """Top-level try/except над агентской цепочкой; делегирует router."""

    def __init__(
        self,
        inner: StreamSource[AgentContext, AgentEvent],
        router: AgentErrorRouter,
    ) -> None:
        self._inner = inner
        self._router = router

    def name(self) -> str:
        return "AgentErrorRouter"

    def reset(self) -> None:
        self._inner.reset()

    def stream(self, ctx: AgentContext) -> Iterable[AgentEvent]:
        try:
            yield from self._inner.stream(ctx)
        except RoutableError as e:
            yield from self._router.route(ctx.request_id, e)
