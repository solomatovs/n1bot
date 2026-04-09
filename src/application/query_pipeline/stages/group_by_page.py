"""Стадия 6: Группировка — ограничение чанков с одной страницы."""
from __future__ import annotations

from typing import Iterator

from application.query_pipeline.events import ChatEvent
from domain.pipeline import StageCompleted, StageStarted
from application.query_pipeline.context import QueryContext
from domain.retrieval import _group_limit_per_page


class GroupByPageStage:

    @property
    def name(self) -> str:
        return "group_by_page"

    def run(self, ctx: QueryContext) -> Iterator[ChatEvent]:
        yield StageStarted(stage=self.name)

        assert ctx.reranked_docs is not None

        ctx.grouped_docs = _group_limit_per_page(
            ctx.reranked_docs, per_page=ctx.search_params.per_page,
        )

        yield StageCompleted(stage=self.name, detail=f"{len(ctx.grouped_docs)} документов после группировки")
