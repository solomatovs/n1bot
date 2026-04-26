"""TurnSpec — fold-фабрика, собирающая LLMRequest из reducer'ов.

Собирается один раз в контейнере: подключаются нужные
reducer'ы, спек готов к многократному build'у.

Per-call:

1. Caller формирует TurnResolveContext из
   AgentContext + MessageService.
2. spec.build(resolve_ctx) → LLMRequest.

Что происходит внутри build:

- initial возвращает пустой TurnState.
- Reducer'ы по возрастанию priority() заполняют slots state'а
  (model, system, messages, tools, sampling). HistoryReducer
  читает свежий снапшот из MessageService — все записи
  предыдущей итерации (assistant, tool_results, feedback) уже
  зафиксированы через DialogueWriter.
- finalize валидирует заполненность обязательных slot'ов
  и возвращает immutable LLMRequest.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from boba.domain.agent.messages import MessageReader
from boba.domain.agent.models import AgentContext
from boba.domain.core.patterns import ContextFoldFactory, StrId
from boba.domain.llm.errors import (
    LLMRequestEmptyMessagesError,
    LLMRequestModelNoneError,
    LLMRequestSystemMessageNoneError,
)
from boba.domain.llm.models import (
    LLMMessage,
    LLMRequest,
    LLMToolRequest,
    SamplingParams,
)


@dataclass
class TurnState:
    model: str | None = None
    system_message: LLMMessage | None = None
    messages: tuple[LLMMessage, ...] = ()
    tools: LLMToolRequest = field(default_factory=LLMToolRequest)
    sampling: SamplingParams = field(default_factory=SamplingParams)
    response_format: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class TurnResolveContext:
    agent: AgentContext
    message_reader: MessageReader


class TurnSpec(
    ContextFoldFactory[TurnResolveContext, StrId, TurnState, LLMRequest],
):
    def initial(self, ctx: TurnResolveContext) -> TurnState:
        return TurnState()

    def finalize(self, ctx: TurnResolveContext, state: TurnState) -> LLMRequest:
        if state.model is None:
            raise LLMRequestModelNoneError()
        if state.system_message is None:
            raise LLMRequestSystemMessageNoneError()
        if not state.messages:
            raise LLMRequestEmptyMessagesError()

        return LLMRequest(
            model=state.model,
            system_message=state.system_message,
            messages=state.messages,
            tools=state.tools,
            sampling=state.sampling,
            response_format=state.response_format,
        )
