"""Полиморфная маршрутизация RoutableError по маркерам."""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from typing import assert_never

from boba.agent.agent import AgentContext
from boba.agent.errors import AgentLLMFeedbackError, RoutableError, UserFeedbackError
from boba.agent.events import AgentEvent, FeedbackToLLMAdded
from boba.agent.messages import MessageWriter
from boba.agent.models import LLMCritique, LLMFeedback, ToolCallRejection
from boba.llm.models import RequestId, UserMessage
from boba.llm.tool_result_render import tool_result_to_message
from boba.patterns import StreamSource
from boba.tools.domain import ErrorResult


class AgentErrorRouter:
    """Маршрутизирует RoutableError по маркерам."""

    def __init__(self, writer: MessageWriter) -> None:
        self._writer = writer

    def route(
        self,
        request_id: RequestId,
        err: RoutableError,
    ) -> Iterator[AgentEvent]:
        if isinstance(err, AgentLLMFeedbackError):
            feedback = err.to_llm_feedback()
            self._dispatch_feedback(feedback)
            yield FeedbackToLLMAdded(
                request_id=request_id,
                content=feedback.content,
            )

        if isinstance(err, UserFeedbackError):
            yield err.to_user_feedback(request_id)

    def _dispatch_feedback(self, feedback: LLMFeedback) -> None:
        """Раскрывает LLMFeedback-union в типизированные Message'и."""
        match feedback:
            case LLMCritique(content=c):
                self._writer.add(UserMessage.from_text(c))
            case ToolCallRejection(tool_call_id=tid, content=c):
                self._writer.add(
                    tool_result_to_message(
                        tool_call_id=tid,
                        result=ErrorResult(
                            message=c,
                            error_kind="ToolCallRejection",
                        ),
                    ),
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
            yield from self._router.route(ctx.request_id, e)
