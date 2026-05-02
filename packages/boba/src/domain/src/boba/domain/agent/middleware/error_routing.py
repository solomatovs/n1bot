"""Полиморфная маршрутизация RoutableError по маркерам."""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from typing import assert_never

from boba.domain.agent.dialogue_writer import DialogueWriter
from boba.domain.agent.errors import AgentLLMFeedbackError
from boba.domain.agent.events import AgentEvent, FeedbackToLLMAdded
from boba.domain.agent.models import AgentContext
from boba.domain.agent.payloads import LLMCritique, LLMFeedback, ToolCallRejection
from boba.domain.core.errors import RoutableError, UserFeedbackError
from boba.domain.llm.models import RequestId
from boba.patterns import StreamSource


class AgentErrorRouter:
    """Маршрутизирует RoutableError по маркерам."""

    def __init__(self, writer: DialogueWriter) -> None:
        self._writer = writer

    def route(
        self,
        ctx: AgentContext,
        err: RoutableError,
    ) -> Iterator[AgentEvent]:
        rid: RequestId = ctx.agent_request.request_id

        if isinstance(err, AgentLLMFeedbackError):
            feedback = err.to_llm_feedback()
            self._dispatch_feedback(feedback)
            yield FeedbackToLLMAdded(
                request_id=rid,
                content=feedback.content,
            )

        if isinstance(err, UserFeedbackError):
            yield err.to_user_feedback(rid)

    def _dispatch_feedback(self, feedback: LLMFeedback) -> None:
        """Раскрывает LLMFeedback-union в узкие writer-методы."""
        match feedback:
            case LLMCritique(content=c):
                self._writer.append_llm_critique(c)
            case ToolCallRejection(tool_call_id=tid, content=c):
                self._writer.append_tool_call_rejection(
                    tool_call_id=tid, content=c,
                )
            case _:
                assert_never(feedback)


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
            yield from self._router.route(ctx, e)
