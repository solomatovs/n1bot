"""Мост между синхронным агентским циклом и async-петлёй Chainlit."""

from __future__ import annotations

import asyncio

from boba.agent.events import AgentEvent

__all__ = ["ChainlitBridgeSink"]


class ChainlitBridgeSink:
    """Sink, перекладывающий AgentEvent в async-очередь из рабочего потока."""

    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        queue: asyncio.Queue[AgentEvent | None],
    ) -> None:
        self._loop = loop
        self._queue = queue

    def handle(self, event: AgentEvent) -> None:
        self._loop.call_soon_threadsafe(self._queue.put_nowait, event)

    def close(self) -> None:
        """Sentinel в очередь: событий больше не будет."""
        self._loop.call_soon_threadsafe(self._queue.put_nowait, None)
