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
    AnswerDelta,
    AnswerMessage,
    GenerationResult,
    InvalidToolCallMessage,
    RefusalDelta,
    RefusalMessage,
    ThinkingDelta,
    ThinkingMessage,
    ToolCallDelta,
    ToolCallMessage,
)
from boba.agent.turn.builder import TurnBuilder
from boba.llm.builder import LLM
from boba.llm.errors import LLMError
from boba.llm.events import (
    LLMAnswerDelta,
    LLMAnswerMessage,
    LLMEvent,
    LLMGenerationResult,
    LLMInvalidToolCallMessage,
    LLMRefusalDelta,
    LLMRefusalMessage,
    LLMThinkingDelta,
    LLMThinkingMessage,
    LLMToolCallDelta,
    LLMToolCallMessage,
)
from boba.llm.models import LLMContext
from boba.patterns import StreamSource


class LLMToAgentConverter:
    """Stateless конвертер LLM → Agent."""

    def convert(self, event: LLMEvent) -> Iterator[AgentEvent]:  # noqa: C901
        match event:
            case LLMThinkingDelta(request_id=rid, token=t):
                yield ThinkingDelta(request_id=rid, token=t)
            case LLMAnswerDelta(request_id=rid, token=t):
                yield AnswerDelta(request_id=rid, token=t)
            case LLMRefusalDelta(request_id=rid, token=t):
                yield RefusalDelta(request_id=rid, token=t)
            case LLMToolCallDelta(
                request_id=rid,
                index=i,
                tool_call_id=tid,
                tool_name=tn,
                arguments=a,
            ):
                yield ToolCallDelta(
                    request_id=rid,
                    index=i,
                    tool_call_id=tid,
                    tool_name=tn,
                    arguments_chunk=a,
                )
            case LLMThinkingMessage(request_id=rid, content=c):
                yield ThinkingMessage(request_id=rid, content=c)
            case LLMAnswerMessage(request_id=rid, content=c):
                yield AnswerMessage(request_id=rid, content=c)
            case LLMRefusalMessage(request_id=rid, content=c):
                yield RefusalMessage(request_id=rid, content=c)
            case LLMToolCallMessage(request_id=rid, call=call):
                yield ToolCallMessage(request_id=rid, call=call)
            case LLMInvalidToolCallMessage(request_id=rid, invalid=invalid):
                yield InvalidToolCallMessage(request_id=rid, invalid=invalid)
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
