"""LLMRequestFactory — fold-фабрика, собирающая LLMRequest из reducer'ов."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from boba.llm.errors import (
    LLMRequestModelNoneError,
    LLMRequestRequestIdNoneError,
)
from boba.llm.models import (
    DialogMessage,
    LLMRequest,
    LLMToolDefinition,
    RequestId,
    SamplingParams,
    SystemMessage,
)
from boba.patterns import FoldFactory


@dataclass
class TurnState:
    request_id: RequestId | None = None
    model: str | None = None
    system_messages: tuple[SystemMessage, ...] = ()
    dialog_messages: tuple[DialogMessage, ...] = ()
    tools: LLMToolDefinition = field(default_factory=LLMToolDefinition)
    sampling: SamplingParams = field(default_factory=SamplingParams)
    response_format: Mapping[str, Any] | None = None


class LLMRequestFactory(
    FoldFactory[str, TurnState, LLMRequest],
):
    def initial(self) -> TurnState:
        return TurnState()

    def finalize(self, state: TurnState) -> LLMRequest:
        if state.request_id is None:
            raise LLMRequestRequestIdNoneError()
        if state.model is None:
            raise LLMRequestModelNoneError()

        return LLMRequest(
            request_id=state.request_id,
            model=state.model,
            system_messages=state.system_messages,
            dialog_messages=state.dialog_messages,
            tools_definition=state.tools,
            sampling=state.sampling,
            response_format=state.response_format,
        )
