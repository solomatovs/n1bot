"""Мост между синхронным агентским циклом и async-петлёй Chainlit."""

from __future__ import annotations

import asyncio

from boba.domain.agent.events import AgentEvent
from boba.domain.agent.models import AgentContext
from boba.domain.core.patterns import StreamSink


class ChainlitBridgeSink(StreamSink[AgentContext, AgentEvent]):
    """Sink, перекладывающий AgentEvent в async-очередь из рабочего потока."""

    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        queue: asyncio.Queue[AgentEvent | None],
    ) -> None:
        self._loop = loop
        self._queue = queue

    def name(self) -> str:
        return "ChainlitBridge"

    def handle(self, ctx: AgentContext, event: AgentEvent) -> None:
        del ctx
        self._loop.call_soon_threadsafe(self._queue.put_nowait, event)

    def close(self) -> None:
        """Sentinel в очередь: событий больше не будет."""
        self._loop.call_soon_threadsafe(self._queue.put_nowait, None)
