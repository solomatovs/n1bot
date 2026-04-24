"""Оркестратор агента — связка ``source → sink``.

S4a: ``run(config, request)`` создаёт контекст с конфигом и
пропускает готовую composed-цепочку. Loop/Boundary/UserQuery-обёртки
навешиваются снаружи (в контейнере), чтобы Agent оставался тонким
«source/sink-драйвером» без знания о внутренностях цепочки.
"""

from __future__ import annotations

from boba.domain.core.patterns import StreamSink, StreamSource
from boba_2.domain.agent.events import AgentEvent
from boba_2.domain.agent.models import AgentConfig, AgentContext, AgentRequest


class Agent:
    """Тонкий оркестратор: создаёт контекст, сливает source в sink."""

    def __init__(
        self,
        source: StreamSource[AgentContext, AgentEvent],
        sink: StreamSink[AgentContext, AgentEvent],
    ) -> None:
        self._source = source
        self._sink = sink

    def name(self) -> str:
        return "Agent"

    def run(self, config: AgentConfig, request: AgentRequest) -> None:
        ctx = AgentContext(agent_request=request, config=config)
        for event in self._source.stream(ctx):
            self._sink.handle(ctx, event)
