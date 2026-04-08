"""Оркестратор пайплайна — последовательное выполнение стадий."""
from __future__ import annotations

import logging
from typing import Generic, Iterator, TypeVar

from domain.pipeline.protocol import PipelineStage

log = logging.getLogger(__name__)

TContext = TypeVar("TContext")
TEvent = TypeVar("TEvent")


class Pipeline(Generic[TContext, TEvent]):
    """Выполняет последовательность стадий PipelineStage.

    Каждая стадия получает контекст и yield-ит события.
    Оркестратор — генератор, ничего не накапливает.

    TContext — тип контекста (QueryContext, LoadContext, ...).
    TEvent — тип событий.
    """

    def __init__(self, stages: list[PipelineStage[TContext, TEvent]]) -> None:
        self._stages = list(stages)

    @property
    def stage_names(self) -> list[str]:
        return [s.name for s in self._stages]

    def run(self, ctx: TContext) -> Iterator[TEvent]:
        """Выполнить все стадии, yield-я события по мере их появления."""
        for stage in self._stages:
            log.debug("Pipeline: stage '%s'", stage.name)
            yield from stage.run(ctx)
