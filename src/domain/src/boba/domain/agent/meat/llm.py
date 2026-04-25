"""
Граница между ответственностью Agent и LLMAgent.

:class:`LLMInvokeMiddleware` — терминал агентской цепочки. На каждую
итерацию:

1. собирает :class:`LLMRequest` через :class:`TurnSpec` из текущего
   снапшота :class:`MessageService`;
2. стримит события LLM-слоя и конвертирует их в :class:`AgentEvent`.
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
from boba.domain.agent.messages import MessageService
from boba.domain.agent.models import AgentContext
from boba.domain.agent.turn.spec import TurnResolveContext, TurnSpec
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
)
from boba.domain.llm.models import LLMContext


class LLMEventToAgentEventConverter(Converter[LLMEvent, AgentEvent]):
    """
    Конвертирует все события LLMEvent в AgentEvent
    """

    def convert(self, value: LLMEvent) -> AgentEvent:  # noqa: C901, PLR0911, PLR0912
        match value:
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
    """Терминал агентской цепочки: build request → invoke LLM.

    Снапшот истории читается :class:`HistoryReducer` напрямую из
    :class:`MessageService` — все записи предыдущей итерации
    (assistant, tool_results, feedback) уже зафиксированы через
    :class:`DialogueWriter`.

    :class:`LLMError` из LLM-слоя оборачивается в
    :class:`LLMGenerationFailedError` (terminal + user-feedback) —
    роутер остановит цикл и эмитит :class:`GenerationFailed`.
    """

    def __init__(
        self,
        llm_source: StreamSource[LLMContext, LLMEvent],
        spec: TurnSpec,
        message_service: MessageService,
    ) -> None:
        self._llm_source = llm_source
        self._spec = spec
        self._message_service = message_service
        self._to_agent = LLMEventToAgentEventConverter()

    def name(self) -> str:
        return "LLMInvoke"

    def stream(self, ctx: AgentContext) -> Iterable[AgentEvent]:
        request = self._spec.build(
            TurnResolveContext(
                agent=ctx,
                message_service=self._message_service,
            )
        )
        try:
            for event in self._llm_source.stream(
                LLMContext(
                    request=request,
                    request_id=ctx.agent_request.request_id,
                )
            ):
                yield self._to_agent.convert(event)
        except LLMError as e:
            raise LLMGenerationFailedError(
                str(e),
                error_kind=type(e).__name__,
            ) from e
