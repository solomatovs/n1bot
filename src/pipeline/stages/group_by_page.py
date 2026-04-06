"""Стадия 6: Группировка — ограничение чанков с одной стра��ицы."""
from __future__ import annotations

from typing import Iterator

from events import ChatEvent
from pipeline.context import PipelineContext
from pipeline.events import StageCompleted, StageStarted
from retrieval import _group_limit_per_page


class GroupByPageStage:

    @property
    def name(self) -> str:
        return "group_by_page"

    def run(self, ctx: PipelineContext) -> Iterator[ChatEvent]:
        yield StageStarted(stage=self.name)

        assert ctx.reranked_docs is not None

        ctx.grouped_docs = _group_limit_per_page(
            ctx.reranked_docs, per_page=ctx.search_params.per_page,
        )

        yield StageCompleted(stage=self.name, detail=f"{len(ctx.grouped_docs)} документов после группировки")
