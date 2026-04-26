"""Мост между синхронным агентским циклом и async-петлёй Chainlit.

ChainlitBridgeSink в агентском потоке (asyncio.to_thread) для каждого
handle() вызывает loop.call_soon_threadsafe — кладёт событие в Queue
основного async-handler'а; async-consumer в app.py итерирует очередь.

Sentinel None сигналит, что агентский поток завершился; решение о
терминальном сообщении принимает consumer по последнему событию.
"""

from __future__ import annotations

import asyncio

from boba.domain.agent.events import AgentEvent
from boba.domain.agent.models import AgentContext
from boba.domain.core.patterns import StreamSink


class ChainlitBridgeSink(StreamSink[AgentContext, AgentEvent]):
    """Sink, перекладывающий AgentEvent в async-очередь из другого потока.

    Владелец очереди — async-handler Chainlit. Sink живёт в рабочем потоке
    (to_thread). call_soon_threadsafe — единственный безопасный
    способ тронуть объекты loop'а из стороннего потока.
    """

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
        """Сигнал async-consumer'у: событий больше не будет.

        Вызывается из того же рабочего потока после agent.run(...)
        (или в finally вокруг него).
        """
        self._loop.call_soon_threadsafe(self._queue.put_nowait, None)
