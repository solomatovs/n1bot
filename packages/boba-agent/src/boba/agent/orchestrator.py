"""Оркестратор агента: stream — поток событий, invoke — прогон до итога."""

from __future__ import annotations

from collections.abc import Iterator

from boba.agent.dialogue_writer import DialogueWriter
from boba.agent.events import AgentEvent, GenerationDone, IterationStarted
from boba.agent.messages import MessageReader
from boba.agent.models import (
    AgentContext,
    AgentInput,
    AgentRunResult,
)
from boba.llm.events import FinishReason
from boba.patterns import StreamSource


class Agent:
    """Тонкий оркестратор: пишет user-query, прогоняет source, отдаёт события."""

    def __init__(
        self,
        source: StreamSource[AgentContext, AgentEvent],
        writer: DialogueWriter,
        reader: MessageReader,
    ) -> None:
        self._source = source
        self._writer = writer
        self._reader = reader

    def name(self) -> str:
        return "Agent"

    def stream(self, input: AgentInput) -> Iterator[AgentEvent]:
        """Прогнать агента; ленивый итератор AgentEvent. Не raise — события."""
        self._writer.append_user_query(input.query)
        ctx = AgentContext(agent_request=input.request, config=input.config)
        yield from self._source.stream(ctx)

    def invoke(self, input: AgentInput) -> AgentRunResult:
        """Прогнать агента до конца; вернуть AgentRunResult."""
        iterations = 0
        finish_reason: FinishReason | None = None
        for event in self.stream(input):
            if isinstance(event, IterationStarted):
                iterations = event.iteration
            elif isinstance(event, GenerationDone):
                finish_reason = event.finish_reason
        return AgentRunResult(
            final_message=self._reader.last(),
            iterations=iterations,
            finish_reason=finish_reason,
        )
