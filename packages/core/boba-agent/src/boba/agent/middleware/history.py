"""HistoryRecorderMiddleware: passthrough перехватчик, пишущий все AgentEvent в журнал."""

from __future__ import annotations

from collections.abc import Iterable

from boba.agent.events import AgentEvent
from boba.agent.history import HistoryWriter
from boba.agent.orchestrator import AgentContext
from boba.patterns import StreamSource


class HistoryRecorderMiddleware(StreamSource[AgentContext, AgentEvent]):
    """Регистрирует каждое AgentEvent из inner-стрима в HistoryService; passthrough."""

    def __init__(
        self,
        inner: StreamSource[AgentContext, AgentEvent],
        writer: HistoryWriter,
    ) -> None:
        self._inner = inner
        self._writer = writer

    def name(self) -> str:
        return "HistoryRecorder"

    def reset(self) -> None:
        self._inner.reset()

    def stream(self, ctx: AgentContext) -> Iterable[AgentEvent]:
        for event in self._inner.stream(ctx):
            self._writer.record(event)
            yield event
