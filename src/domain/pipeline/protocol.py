"""Протокол стадии пайплайна — generic база для всех пайплайнов."""
from __future__ import annotations

from typing import Iterator, Protocol, TypeVar, runtime_checkable

TContext = TypeVar("TContext", contravariant=True)
TEvent = TypeVar("TEvent", covariant=True)


@runtime_checkable
class PipelineStage(Protocol[TContext, TEvent]):
    """Одна стадия пайплайна.

    Каждая стадия читает входные данные из ctx, записывает результаты,
    и yield-ит события для наблюдаемости.

    TContext — тип контекста (QueryContext, LoadContext, ...).
    TEvent — тип событий, которые yield-ит стадия.
    """

    @property
    def name(self) -> str: ...

    def run(self, ctx: TContext) -> Iterator[TEvent]: ...
