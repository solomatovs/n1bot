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

    def stream(self, agent_input: AgentInput) -> Iterator[AgentEvent]:
        """Прогнать агента; ленивый итератор AgentEvent. Не raise — события."""
        self._source.reset()
        self._writer.append_user_query(agent_input.request.query)
        ctx = AgentContext(request=agent_input.request, config=agent_input.config)
        yield from self._source.stream(ctx)

    def invoke(self, agent_input: AgentInput) -> AgentRunResult:
        """Прогнать агента до конца; вернуть AgentRunResult."""
        iterations = 0
        finish_reason: FinishReason | None = None
        for event in self.stream(agent_input):
            if isinstance(event, IterationStarted):
                iterations = event.iteration
            elif isinstance(event, GenerationDone):
                finish_reason = event.finish_reason
        return AgentRunResult(
            final_message=self._reader.last(),
            iterations=iterations,
            finish_reason=finish_reason,
        )
