"""TurnSpec — fold-фабрика, собирающая LLMRequest из reducer'ов."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from boba.llm.errors import (
    LLMRequestEmptyMessagesError,
    LLMRequestModelNoneError,
    LLMRequestSystemMessageNoneError,
)
from boba.llm.models import (
    LLMRequest,
    LLMToolRequest,
    Message,
    RequestId,
    SamplingParams,
    SystemMessage,
)
from boba.patterns import FoldFactory


@dataclass
class TurnState:
    model: str | None = None
    system_message: SystemMessage | None = None
    messages: tuple[Message, ...] = ()
    tools: LLMToolRequest = field(default_factory=LLMToolRequest)
    sampling: SamplingParams = field(default_factory=SamplingParams)
    response_format: Mapping[str, Any] | None = None


class TurnSpec(
    FoldFactory[str, TurnState, LLMRequest],
):
    def __init__(self, request_id: RequestId) -> None:
        super().__init__()
        self._request_id = request_id

    def initial(self) -> TurnState:
        return TurnState()

    def finalize(self, state: TurnState) -> LLMRequest:
        if state.model is None:
            raise LLMRequestModelNoneError()
        if state.system_message is None:
            raise LLMRequestSystemMessageNoneError()
        if not state.messages:
            raise LLMRequestEmptyMessagesError()

        return LLMRequest(
            request_id=self._request_id,
            model=state.model,
            system_message=state.system_message,
            messages=state.messages,
            tools=state.tools,
            sampling=state.sampling,
            response_format=state.response_format,
        )
