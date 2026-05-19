"""
Граница (port) между Agent-слоем и LLM-слоем
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from typing import assert_never

from boba.agent.agent import AgentContext
from boba.agent.errors import LLMGenerationFailedError
from boba.agent.events import (
    AgentEvent,
    AnswerComplete,
    AnswerStarted,
    AnswerToken,
    GenerationDone,
    GenerationResult,
    GenerationRetried,
    GenerationStarted,
    InvalidToolCallReceived,
    RefusalComplete,
    RefusalToken,
    ResponseStarted,
    ThinkingComplete,
    ThinkingStarted,
    ThinkingToken,
    ToolCallArgumentDelta,
    ToolCallComplete,
    ToolCallStreamStarted,
)
from boba.agent.events import RequestStart as AgentLLMRequestSent
from boba.agent.turn.builder import TurnBuilder
from boba.llm.builder import LLM
from boba.llm.errors import LLMError
from boba.llm.events import (
    LLMAnswerComplete,
    LLMAnswerStarted,
    LLMAnswerToken,
    LLMEvent,
    LLMGenerationDone,
    LLMGenerationResult,
    LLMGenerationStarted,
    LLMInvalidToolCallReceived,
    LLMRefusalComplete,
    LLMRefusalToken,
    LLMRequestStarted,
    LLMResponseStarted,
    LLMRetryAttempt,
    LLMThinkingComplete,
    LLMThinkingStarted,
    LLMThinkingToken,
    LLMToolCallArgumentDelta,
    LLMToolCallBegin,
    LLMToolCallComplete,
)
from boba.llm.models import LLMContext
from boba.patterns import StreamSource


class LLMToAgentConverter:
    """Stateless конвертер LLM → Agent."""

    def convert(self, event: LLMEvent) -> Iterator[AgentEvent]:  # noqa: C901, PLR0912
        match event:
            case LLMRequestStarted(
                request_id=rid,
                model=model,
                has_tools=has_tools,
                monotonic_ns=ts,
            ):
                yield AgentLLMRequestSent(
                    request_id=rid,
                    model=model,
                    has_tools=has_tools,
                    monotonic_ns=ts,
                )
            case LLMResponseStarted(request_id=rid, monotonic_ns=ts):
                yield ResponseStarted(request_id=rid, monotonic_ns=ts)
            case LLMGenerationStarted(request_id=rid):
                yield GenerationStarted(request_id=rid)
            case LLMThinkingStarted(request_id=rid):
                yield ThinkingStarted(request_id=rid)
            case LLMThinkingToken(request_id=rid, token=t):
                yield ThinkingToken(request_id=rid, token=t)
            case LLMAnswerStarted(request_id=rid):
                yield AnswerStarted(request_id=rid)
            case LLMAnswerToken(request_id=rid, token=t):
                yield AnswerToken(request_id=rid, token=t)
            case LLMRefusalToken(request_id=rid, token=t):
                yield RefusalToken(request_id=rid, token=t)
            case LLMToolCallBegin(
                request_id=rid,
                index=i,
                tool_call_id=tid,
                tool_name=tn,
            ):
                yield ToolCallStreamStarted(
                    request_id=rid,
                    index=i,
                    tool_call_id=tid,
                    tool_name=tn,
                )
            case LLMToolCallArgumentDelta(
                request_id=rid,
                index=i,
                tool_call_id=tid,
                tool_name=tn,
                arguments=a,
            ):
                yield ToolCallArgumentDelta(
                    request_id=rid,
                    index=i,
                    tool_call_id=tid,
                    tool_name=tn,
                    arguments_chunk=a,
                )
            case LLMRetryAttempt(
                request_id=rid,
                attempt=att,
                reason=rsn,
                status_code=sc,
            ):
                yield GenerationRetried(
                    request_id=rid,
                    attempt=att,
                    reason=rsn,
                    status_code=sc,
                )
            case LLMGenerationDone(request_id=rid, finish_reason=fr):
                yield GenerationDone(request_id=rid, finish_reason=fr)
            case LLMThinkingComplete(request_id=rid, content=c):
                yield ThinkingComplete(request_id=rid, content=c)
            case LLMAnswerComplete(request_id=rid, content=c):
                yield AnswerComplete(request_id=rid, content=c)
            case LLMRefusalComplete(request_id=rid, content=c):
                yield RefusalComplete(request_id=rid, content=c)
            case LLMToolCallComplete(request_id=rid, call=call):
                yield ToolCallComplete(request_id=rid, call=call)
            case LLMInvalidToolCallReceived(request_id=rid, invalid=invalid):
                yield InvalidToolCallReceived(request_id=rid, invalid=invalid)
            case LLMGenerationResult(
                request_id=rid,
                message=msg,
                finish_reason=fr,
            ):
                yield GenerationResult(
                    request_id=rid,
                    message=msg,
                    finish_reason=fr,
                )
            case _:
                assert_never(event)


class LLMPort(StreamSource[AgentContext, AgentEvent]):
    """
    Agent собирает LLMRequest и отправляет на обработку в LLM

    LLMPort выполняет функции:
        * собирает LLMRequest и отправляет в LLM слой
        * конвертирует события приходящие от LLM в агентские события
            LLMEvent -> AgentEvent
        * конвертирует ошибки приходящие от LLM в агентские ошибки

    LLMPort это единственное место в Agent-цепочке,
    которое знает об существовании LLM
    """

    def __init__(
        self,
        llm: LLM,
        turn: TurnBuilder,
    ) -> None:
        self._llm = llm
        self._turn = turn

    def name(self) -> str:
        return "LLMPort"

    def stream(self, ctx: AgentContext) -> Iterable[AgentEvent]:
        request = self._turn.build(ctx)

        try:
            for event in self._llm.stream(LLMContext(request=request)):
                yield from LLMToAgentConverter().convert(event)
        except LLMError as e:
            # Подробности (status_code + цепочка причин) автоматически
            # обогащаются в `AgentErrorRouter` из `__cause__` — здесь
            # достаточно бросить ошибку «from e».
            raise LLMGenerationFailedError(
                str(e),
                error_kind=type(e).__name__,
            ) from e
