"""TurnSpec — fold-фабрика, собирающая LLMRequest из reducer'ов."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from boba.llm.errors import (
    LLMRequestEmptyMessagesError,
    LLMRequestModelNoneError,
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
    model: str | None = None
    system_messages: tuple[SystemMessage, ...] = ()
    dialog_messages: tuple[DialogMessage, ...] = ()
    tools: LLMToolDefinition = field(default_factory=LLMToolDefinition)
    sampling: SamplingParams = field(default_factory=SamplingParams)
    response_format: Mapping[str, Any] | None = None


class LLMRequestFactory(
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
        if not state.dialog_messages:
            raise LLMRequestEmptyMessagesError()

        return LLMRequest(
            request_id=self._request_id,
            model=state.model,
            system_messages=state.system_messages,
            dialog_messages=state.dialog_messages,
            tools_definition=state.tools,
            sampling=state.sampling,
            response_format=state.response_format,
        )
