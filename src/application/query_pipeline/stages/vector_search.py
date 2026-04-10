"""Стадия 3: Векторный поиск по каждому варианту запроса."""
from __future__ import annotations

import logging
from typing import Iterator

from application.query_pipeline.events import ChatEvent
from domain.errors import VectorStoreError
from domain.pipeline import StageCompleted, StageStarted
from application.query_pipeline.context import QueryContext
from domain.retrieval import build_search_filter

log = logging.getLogger(__name__)


class VectorSearchStage:

    @property
    def name(self) -> str:
        return "vector_search"

    def run(self, ctx: QueryContext) -> Iterator[ChatEvent]:
        yield StageStarted(stage=self.name)

        assert ctx.query_type is not None
        assert ctx.query_variants is not None

        use_multi = ctx.search_params.use_multi_query
        k = ctx.search_params.k_per_variant if use_multi else ctx.search_params.top_n

        rank_lists: list[list] = []
        errors: list[str] = []

        filters = build_search_filter(ctx.search_params.content_types, ctx.query_type)
        for variant in ctx.query_variants:
            try:
                results = ctx.vectorstore_service.search(ctx.collection_name, variant, k, filters)
                rank_lists.append(results)
            except VectorStoreError as e:
                log.warning("Search failed for variant '%s': %s", variant, e)
                errors.append(f"'{variant}': {e}")
                rank_lists.append([])

        ctx.rank_lists = rank_lists
        ctx.search_errors = errors

        total_docs = sum(len(rl) for rl in rank_lists)
        yield StageCompleted(
            stage=self.name,
            detail=f"{total_docs} документов из {len(rank_lists)} вариантов",
        )
