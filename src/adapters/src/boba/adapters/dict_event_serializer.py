"""Dict-реализация конвертеров AgentEvent через dataclasses."""

from __future__ import annotations

from dataclasses import asdict

from boba.domain.core.patterns import Converter
from boba.domain.agent.events import (
    AgentEvent,
    AnswerStarted,
    AnswerToken,
    GenerationDone,
    GenerationStarted,
    RefusalToken,
    StageCompleted,
    StageStarted,
    ThinkingStarted,
    ThinkingToken,
    ToolCallArgumentDelta,
    ToolCallBegin,
    ToolResultReady,
    UserQueryReceived,
)

_EVENT_TYPES: dict[str, type] = {
    cls.__name__: cls
    for cls in (
        UserQueryReceived,
        StageStarted,
        StageCompleted,
        GenerationStarted,
        ThinkingStarted,
        ThinkingToken,
        AnswerStarted,
        AnswerToken,
        RefusalToken,
        GenerationDone,
        ToolCallBegin,
        ToolCallArgumentDelta,
        ToolResultReady,
    )
}


class AgentEventEncoder(Converter[AgentEvent, tuple[str, dict]]):
    """AgentEvent → (event_type, event_data) через dataclasses.asdict."""

    def convert(self, value: AgentEvent) -> tuple[str, dict]:
        return type(value).__name__, asdict(value)


class AgentEventDecoder(Converter[tuple[str, dict], AgentEvent]):
    """(event_type, event_data) → AgentEvent."""

    def convert(self, value: tuple[str, dict]) -> AgentEvent:
        event_type, event_data = value
        cls = _EVENT_TYPES.get(event_type)
        if cls is None:
            raise ValueError(f"Unknown event type: {event_type}")
        return cls(**event_data)
