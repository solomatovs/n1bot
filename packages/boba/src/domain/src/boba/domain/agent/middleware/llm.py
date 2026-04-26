"""Граница между ответственностью Agent и LLM-слоя.

:class:`LLMInvokeMiddleware` — терминал агентской цепочки. На каждую
итерацию:

1. собирает :class:`LLMRequest` через :class:`TurnSpec` из текущего
   снапшота :class:`MessageService`;
2. стримит события LLM-слоя и конвертирует их в :class:`AgentEvent`.

Маппинг LLM → Agent stateful: на стороне агента ``LLMRequestSent``
означает «round-trip отправлен», а stream-handle получен — это
``LLMResponseStreamOpened``. Конвертер живёт замыканием вокруг
текущего ``LLMRequest`` — он его не несёт в события, но использует
для обогащения метаданными round-trip'а.

Для :class:`ToolCallArgumentDelta` конвертер запоминает соответствие
``index → (tool_call_id, tool_name)`` из предшествующего
:class:`LLMToolCallBegin` — это нужно, чтобы delta-события на агент-
уровне были self-sufficient (несли имя tool'а, а не только index).
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from typing import assert_never

from boba.domain.agent.errors import LLMGenerationFailedError
from boba.domain.agent.events import (
    AgentEvent,
    AnswerStarted,
    AnswerToken,
    GenerationDone,
    GenerationRetried,
    GenerationStarted,
    LLMResponseStreamOpened,
    RefusalToken,
    ThinkingStarted,
    ThinkingToken,
    ToolCallArgumentDelta,
    ToolCallStreamStarted,
)
from boba.domain.agent.events import LLMRequestSent as AgentLLMRequestSent
from boba.domain.agent.messages import MessageService
from boba.domain.agent.models import AgentContext
from boba.domain.agent.turn.spec import TurnResolveContext, TurnSpec
from boba.domain.core.patterns import StreamSource
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
from boba.domain.llm.models import LLMContext, LLMRequest


class _LLMToAgentConverter:
    """Per-stream stateful конвертер LLM → Agent.

    Состояние: ``index → (tool_call_id, tool_name)`` для обогащения
    :class:`ToolCallArgumentDelta` именем tool'а (на LLM-уровне у delta
    есть только ``index``; ``LLMToolCallBegin`` несёт ``id`` и ``name``).
    Состояние локально для одного round-trip'а — конвертер создаётся
    заново на каждый ``stream()``.
    """

    def __init__(self, request: LLMRequest) -> None:
        self._request = request
        self._tool_calls: dict[int, tuple[str, str]] = {}

    def convert(self, event: LLMEvent) -> Iterator[AgentEvent]:  # noqa: C901, PLR0912
        match event:
            case LLMRequestStarted(request_id=rid, monotonic_ns=ts):
                yield AgentLLMRequestSent(
                    request_id=rid,
                    model=self._request.model,
                    messages_count=self._request.messages_count(),
                    has_tools=self._request.has_tools(),
                    monotonic_ns=ts,
                )
            case LLMRequestSent(request_id=rid, monotonic_ns=ts):
                yield LLMResponseStreamOpened(request_id=rid, monotonic_ns=ts)
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

    def name(self) -> str:
        return "LLMInvoke"

    def stream(self, ctx: AgentContext) -> Iterable[AgentEvent]:
        request = self._spec.build(
            TurnResolveContext(
                agent=ctx,
                message_service=self._message_service,
            )
        )
        converter = _LLMToAgentConverter(request)
        try:
            for event in self._llm_source.stream(
                LLMContext(
                    request=request,
                    request_id=ctx.agent_request.request_id,
                )
            ):
                yield from converter.convert(event)
        except LLMError as e:
            raise LLMGenerationFailedError(
                str(e),
                error_kind=type(e).__name__,
            ) from e
