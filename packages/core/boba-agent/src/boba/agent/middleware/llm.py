"""Граница (port) между Agent-слоем и LLM-слоем.

`LLMPort` — adapter в смысле ports-and-adapters: единственное место, где
Agent-слой знает про существование LLM-слоя. Слой LLM в обратную сторону
ничего про Agent не знает.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from typing import assert_never

from boba.agent.agent import AgentContext
from boba.agent.errors import LLMGenerationFailedError
from boba.agent.events import (
    AgentEvent,
    AnswerStarted,
    AnswerToken,
    GenerationDone,
    GenerationRetried,
    GenerationStarted,
    RefusalToken,
    ResponseStarted,
    ThinkingStarted,
    ThinkingToken,
    ToolCallArgumentDelta,
    ToolCallStreamStarted,
)
from boba.agent.events import RequestStart as AgentLLMRequestSent
from boba.agent.turn.builder import TurnSpecBuilder
from boba.llm.builder import LLM
from boba.llm.errors import LLMError
from boba.llm.events import (
    LLMAnswerStarted,
    LLMAnswerToken,
    LLMEvent,
    LLMGenerationDone,
    LLMGenerationStarted,
    LLMRefusalToken,
    LLMRequestStarted,
    LLMResponseStarted,
    LLMRetryAttempt,
    LLMThinkingStarted,
    LLMThinkingToken,
    LLMToolCallArgumentDelta,
    LLMToolCallBegin,
)
from boba.llm.models import LLMContext, LLMRequest
from boba.patterns import StreamSource


class LLMToAgentConverter:
    """Per-stream stateful конвертер LLM → Agent."""

    def __init__(self, request: LLMRequest) -> None:
        self._request = request
        self._tool_calls: dict[int, tuple[str, str]] = {}

    def convert(self, event: LLMEvent) -> Iterator[AgentEvent]:  # noqa: C901, PLR0912
        match event:
            case LLMRequestStarted(request_id=rid, monotonic_ns=ts):
                yield AgentLLMRequestSent(
                    request_id=rid,
                    model=self._request.model,
                    has_tools=self._request.has_tools(),
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
                self._tool_calls[i] = (tid, tn)
                yield ToolCallStreamStarted(
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
                tid, tn = self._tool_calls.get(i, ("", ""))
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
            case _:
                assert_never(event)


class LLMPort(StreamSource[AgentContext, AgentEvent]):
    """
    Agent программа собирает LLMRequest и отправляет на обработку в LLM программе

    LLMPort выполняет две функции:
    - собирает LLMRequest и отправляет в LLM слой
    - конвертирует события приходящие от LLM в агентские события
        LLMEvent -> AgentEvent
    - конвертирует ошибки приходящие от LLM в агентские ошибки

    LLMPort это единственное место в Agent-цепочке,
    которое знает об существовании LLM
    """

    def __init__(
        self,
        llm: LLM,
        turn_spec_builder: TurnSpecBuilder,
    ) -> None:
        self._llm = llm
        self._turn_spec_builder = turn_spec_builder

    def name(self) -> str:
        return "LLMPort"

    def stream(self, ctx: AgentContext) -> Iterable[AgentEvent]:
        request = self._turn_spec_builder.build(ctx).build()

        try:
            converter = LLMToAgentConverter(request)

            for event in self._llm.stream(LLMContext(request=request)):
                yield from converter.convert(event)
        except LLMError as e:
            raise LLMGenerationFailedError(
                str(e),
                error_kind=type(e).__name__,
            ) from e
