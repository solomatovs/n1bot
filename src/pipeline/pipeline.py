"""Оркестратор пайплайна — последовательное выполнение стадий."""
from __future__ import annotations

import logging
from typing import Iterator

from events import ChatEvent, RetrievalStarted
from pipeline.context import PipelineContext
from pipeline.protocol import PipelineStage

log = logging.getLogger(__name__)


class QueryPipeline:
    """Выполняет последовательность стадий PipelineStage.

    Каждая стадия получает один PipelineContext и yield-ит ChatEvent.
    Оркестратор — генератор, ничего не накапливает.
    """

    def __init__(self, stages: list[PipelineStage]) -> None:
        self._stages = list(stages)

    @property
    def stage_names(self) -> list[str]:
        return [s.name for s in self._stages]

    def run(self, ctx: PipelineContext) -> Iterator[ChatEvent]:
        """Выполнить все стадии, yield-я события по мере их появления."""
        yield RetrievalStarted(query=ctx.query, collection=ctx.collection_name)

        for stage in self._stages:
            log.debug("Pipeline: стадия '%s'", stage.name)
            yield from stage.run(ctx)
