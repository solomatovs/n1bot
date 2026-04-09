"""Стадия 2: Поиск релевантных чанков по запросу пользователя."""
from __future__ import annotations

import logging
from typing import Iterator

from application.doc_pipeline.context import DocPipelineContext
from application.doc_pipeline.events import DocPipelineEvent, SearchDone
from domain.doc_search import ChunkLocation, SearchHit
from domain.pipeline import StageCompleted, StageStarted

log = logging.getLogger(__name__)


class SearchStage:
    """Ищет top-K чанков через VectorStoreService."""

    @property
    def name(self) -> str:
        return "search"

    def run(self, ctx: DocPipelineContext) -> Iterator[DocPipelineEvent]:
        yield StageStarted(stage=self.name)

        results = ctx.vectorstore_service.search_with_scores(
            ctx.collection_name, ctx.query, ctx.top_k,
        )

        hits = []
        for scored in results:
            meta = scored.document.metadata
            location = ChunkLocation(
                source_file=meta.get("source_file", ""),
                start_line=meta.get("start_line", 0),
                end_line=meta.get("end_line", 0),
                section_title=meta.get("section_title", ""),
            )
            hits.append(SearchHit(
                content=scored.document.page_content,
                location=location,
                score=scored.score,
            ))

        ctx.hits = hits
        yield SearchDone(hits=hits)
        yield StageCompleted(
            stage=self.name,
            detail=f"найдено {len(hits)} чанков",
        )
