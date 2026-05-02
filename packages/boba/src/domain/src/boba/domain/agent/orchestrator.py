"""Оркестратор агента — связка source → sink."""

from __future__ import annotations

from boba.domain.agent.dialogue_writer import DialogueWriter
from boba.domain.agent.events import AgentEvent
from boba.domain.agent.models import AgentConfig, AgentContext, AgentRequest
from boba.patterns import StreamSink, StreamSource


class Agent:
    """Тонкий оркестратор: пишет user-message, создаёт ctx, source → sink."""

    def __init__(
        self,
        source: StreamSource[AgentContext, AgentEvent],
        sink: StreamSink[AgentContext, AgentEvent],
        writer: DialogueWriter,
    ) -> None:
        self._source = source
        self._sink = sink
        self._writer = writer

    def name(self) -> str:
        return "Agent"

    def run(
        self,
        config: AgentConfig,
        request: AgentRequest,
        query: str,
    ) -> None:
        self._writer.append_user_query(query)
        ctx = AgentContext(agent_request=request, config=config)
        for event in self._source.stream(ctx):
            self._sink.handle(ctx, event)
