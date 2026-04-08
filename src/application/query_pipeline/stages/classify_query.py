"""Стадия 1: Классификация запроса."""
from __future__ import annotations

from typing import Iterator

from application.query_pipeline.events import ChatEvent
from domain.pipeline.events import StageCompleted, StageStarted
from application.query_pipeline.context import QueryContext
from domain.retrieval import _classify_query_type


class ClassifyQueryStage:

    @property
    def name(self) -> str:
        return "classify_query"

    def run(self, ctx: QueryContext) -> Iterator[ChatEvent]:
        yield StageStarted(stage=self.name)
        ctx.query_type = _classify_query_type(ctx.query)
        yield StageCompleted(stage=self.name, detail=f"type={ctx.query_type}")
