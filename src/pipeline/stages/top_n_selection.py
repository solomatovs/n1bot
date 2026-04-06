"""Стадия 7: Отбор top-N документов для контекста."""
from __future__ import annotations

from typing import Iterator

from errors import EmptyContextError
from events import ChatEvent
from pipeline.context import PipelineContext
from pipeline.events import StageCompleted, StageStarted


class TopNSelectionStage:

    @property
    def name(self) -> str:
        return "top_n_selection"

    def run(self, ctx: PipelineContext) -> Iterator[ChatEvent]:
        yield StageStarted(stage=self.name)

        source = ctx.grouped_docs if ctx.grouped_docs is not None else ctx.merged_docs
        assert source is not None

        ctx.selected_docs = source[:ctx.search_params.answers_per_variant]

        if not ctx.selected_docs:
            raise EmptyContextError("Не найдено релевантных документов по запросу.")

        yield StageCompleted(stage=self.name, detail=f"{len(ctx.selected_docs)} документов отобрано")
