"""Протокол стадии пайплайна."""
from __future__ import annotations

from typing import TYPE_CHECKING, Iterator, Protocol, runtime_checkable

if TYPE_CHECKING:
    from events import ChatEvent
    from pipeline.context import PipelineContext


@runtime_checkable
class PipelineStage(Protocol):
    """Одна стадия RAG-пайплайна.

    Каждая стадия читает входные данные из ctx, записывает результаты,
    и yield-ит события для наблюдаемости.
    """

    @property
    def name(self) -> str: ...

    def run(self, ctx: PipelineContext) -> Iterator[ChatEvent]: ...
