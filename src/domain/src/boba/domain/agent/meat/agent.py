"""Оркестратор цикла агента."""

from __future__ import annotations

from boba.domain.agent.events import AgentEvent
from boba.domain.agent.models import AgentConfig, AgentContext, AgentRequest
from boba.domain.core.patterns import StreamSink, StreamSourceLoop


class Agent:
    def __init__(
        self,
        source: StreamSourceLoop[AgentContext, AgentEvent],
        sink: StreamSink[AgentContext, AgentEvent],
    ) -> None:
        self._source = source
        self._sink = sink

    def name(self) -> str:
        return "AgentLoop"

    def run(self, config: AgentConfig, request: AgentRequest):
        """
        Запускает цикл обработки запроса агентом.
        """
        ctx = AgentContext(
            request=request,
            config=config,
        )

        for event in self._source.stream(ctx):
            self._sink.handle(ctx, event)
