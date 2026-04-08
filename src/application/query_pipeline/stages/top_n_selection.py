"""Стадия 7: Отбор top-N документов для контекста."""
from __future__ import annotations

from typing import Iterator

from domain.search import EmptyContextError
from application.query_pipeline.events import ChatEvent
from domain.pipeline.events import StageCompleted, StageStarted
from application.query_pipeline.context import QueryContext


class TopNSelectionStage:

    @property
    def name(self) -> str:
        return "top_n_selection"

    def run(self, ctx: QueryContext) -> Iterator[ChatEvent]:
        yield StageStarted(stage=self.name)

        source = ctx.grouped_docs if ctx.grouped_docs is not None else ctx.merged_docs
        assert source is not None

        ctx.selected_docs = source[:ctx.search_params.answers_per_variant]

        if not ctx.selected_docs:
            raise EmptyContextError("No relevant documents found for the query.")

        yield StageCompleted(stage=self.name, detail=f"{len(ctx.selected_docs)} документов отобрано")
