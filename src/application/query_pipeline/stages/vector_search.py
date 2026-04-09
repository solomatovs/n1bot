"""Стадия 3: Векторный поиск по каждому варианту запроса."""
from __future__ import annotations

import logging
from typing import Iterator

import chromadb.errors

from application.query_pipeline.events import ChatEvent
from domain.pipeline import StageCompleted, StageStarted
from application.query_pipeline.context import QueryContext
from domain.retrieval import _build_search_filter

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

        filters = _build_search_filter(ctx.search_params.content_types, ctx.query_type)
        vectorstore = ctx.vectorstore_service.get_vectorstore(ctx.collection_name)
        for variant in ctx.query_variants:
            try:
                retr = vectorstore.as_retriever(search_kwargs={"k": k, "filter": filters})
                results = retr.invoke(variant)
                if results is None:
                    results = []
                rank_lists.append(results)
            except (chromadb.errors.ChromaError, RuntimeError) as e:
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
