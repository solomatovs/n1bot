"""
Граница между ответственностью Agent и LLMAgent
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import assert_never

from boba.domain.agent.errors import LLMGenerationFailedError
from boba.domain.agent.events import (
    AgentEvent,
    AnswerStarted,
    AnswerToken,
    GenerationDone,
    GenerationRetried,
    GenerationStarted,
    RefusalToken,
    ThinkingStarted,
    ThinkingToken,
    ToolCallArgumentDelta,
    ToolCallBegin,
)
from boba.domain.agent.events import LLMRequestSent as AgentLLMRequestSent
from boba.domain.agent.events import LLMRequestStarted as AgentLLMRequestStarted
from boba.domain.agent.events import LLMUserPromptIssued as AgentLLMUserPromptIssued
from boba.domain.agent.models import AgentContext
from boba.domain.core.patterns import Converter, StreamSource
from boba.domain.llm.errors import LLMError
from boba.domain.llm.events import (
    LLMAnswerStarted,
    LLMAnswerToken,
    LLMEvent,
    LLMGenerationDone,
    LLMGenerationStarted,
    LLMRefusalToken,
    LLMRequestSent,
    LLMRequestStarted,
    LLMRetryAttempt,
    LLMThinkingStarted,
    LLMThinkingToken,
    LLMToolCallArgumentDelta,
    LLMToolCallBegin,
    LLMUserPromptIssued,
)
from boba.domain.llm.models import LLMContext, LLMRequestBuilder


class LLMEventToAgentEventConverter(Converter[LLMEvent, AgentEvent]):
    """
    Конвертирует все события LLMEvent в AgentEvent
    """

    def convert(self, value: LLMEvent) -> AgentEvent:  # noqa: C901, PLR0911, PLR0912
        match value:
            case LLMUserPromptIssued(request_id=rid, user_prompt=up):
                return AgentLLMUserPromptIssued(
                    request_id=rid,
                    user_prompt=up,
                )
            case LLMRequestStarted(
                request_id=rid,
                model=m,
                messages_count=mc,
                has_tools=ht,
                monotonic_ns=ts,
            ):
                return AgentLLMRequestStarted(
                    request_id=rid,
                    model=m,
                    messages_count=mc,
                    has_tools=ht,
                    monotonic_ns=ts,
                )
            case LLMRequestSent(request_id=rid, monotonic_ns=ts):
                return AgentLLMRequestSent(
                    request_id=rid,
                    monotonic_ns=ts,
                )
            case LLMGenerationStarted(request_id=rid):
                return GenerationStarted(request_id=rid)
            case LLMThinkingStarted(request_id=rid):
                return ThinkingStarted(request_id=rid)
            case LLMThinkingToken(request_id=rid, token=t):
                return ThinkingToken(request_id=rid, token=t)
            case LLMAnswerStarted(request_id=rid):
                return AnswerStarted(request_id=rid)
            case LLMAnswerToken(request_id=rid, token=t):
                return AnswerToken(request_id=rid, token=t)
            case LLMRefusalToken(request_id=rid, token=t):
                return RefusalToken(request_id=rid, token=t)
            case LLMToolCallBegin(
                request_id=rid,
                index=i,
                tool_call_id=tid,
                tool_name=tn,
            ):
                return ToolCallBegin(
                    request_id=rid,
                    index=i,
                    tool_call_id=tid,
                    tool_name=tn,
                )
            case LLMToolCallArgumentDelta(
                request_id=rid,
                index=i,
                arguments=a,
            ):
                return ToolCallArgumentDelta(
                    request_id=rid,
                    index=i,
                    arguments=a,
                )
            case LLMRetryAttempt(
                request_id=rid,
                attempt=att,
                reason=rsn,
                status_code=sc,
            ):
                return GenerationRetried(
                    request_id=rid,
                    attempt=att,
                    reason=rsn,
                    status_code=sc,
                )
            case LLMGenerationDone(request_id=rid, finish_reason=fr):
                return GenerationDone(request_id=rid, finish_reason=fr)
            case _:
                assert_never(value)


class LLMInvokeMiddleware(StreamSource[AgentContext, AgentEvent]):
    """
    Вызывает LLMAgent для обработки LLMRequest
    собранного Agent'ом по Middleware цепочке ранее
    """

    def __init__(
        self,
        llm_source: StreamSource[LLMContext, LLMEvent],
    ) -> None:
        self._llm_source = llm_source
        self._to_agent = LLMEventToAgentEventConverter()

    def name(self) -> str:
        return "LLMInvoke"

    def stream(self, ctx: AgentContext) -> Iterable[AgentEvent]:
        try:
            for event in self._llm_source.stream(
                LLMContext(
                    request=ctx.request.build(),
                    request_id=ctx.agent_request.request_id,
                )
            ):
                yield self._to_agent.convert(event)
        except LLMError as e:
            raise LLMGenerationFailedError(
                str(e),
                error_kind=type(e).__name__,
            ) from e


class NewLLMRequestMiddleware(StreamSource[AgentContext, AgentEvent]):
    """"""

    def __init__(self, inner: StreamSource[AgentContext, AgentEvent]) -> None:
        self._inner = inner

    def name(self) -> str:
        return "IterationCounter"

    def reset(self) -> None:
        self._inner.reset()

    def stream(self, ctx: AgentContext) -> Iterable[AgentEvent]:
        ctx.request = LLMRequestBuilder()
        yield from self._inner.stream(ctx)
