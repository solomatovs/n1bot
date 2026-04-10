"""Стадия 5: Реранкинг документов по метаданным."""
from __future__ import annotations

from typing import Iterator

from application.query_pipeline.events import ChatEvent
from domain.pipeline import StageCompleted, StageStarted
from application.query_pipeline.context import QueryContext
from domain.retrieval import rerank_results


class RerankStage:

    @property
    def name(self) -> str:
        return "rerank"

    def run(self, ctx: QueryContext) -> Iterator[ChatEvent]:
        yield StageStarted(stage=self.name)

        assert ctx.merged_docs is not None
        assert ctx.query_type is not None

        ctx.reranked_docs = rerank_results(ctx.merged_docs, ctx.query_type)

        yield StageCompleted(stage=self.name, detail=f"{len(ctx.reranked_docs)} документов отранжировано")
