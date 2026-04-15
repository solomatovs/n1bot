"""Fan-out sink: раздаёт каждое событие всем вложенным sink'ам."""

from __future__ import annotations

from typing import TypeVar

from boba.domain.core.patterns import StreamSink

TContext = TypeVar("TContext")
TEvent = TypeVar("TEvent")


class CompositeSink(StreamSink[TContext, TEvent]):
    """Fan-out: раздаёт каждое событие всем вложенным sink'ам."""

    def __init__(self, sinks: list[StreamSink[TContext, TEvent]]) -> None:
        self._sinks = list(sinks)

    def name(self) -> str:
        return "CompositeSink(" + ", ".join(s.name() for s in self._sinks) + ")"

    def reset(self) -> None:
        for sink in self._sinks:
            sink.reset()

    def handle(self, event: TEvent) -> None:
        for sink in self._sinks:
            sink.handle(event)
