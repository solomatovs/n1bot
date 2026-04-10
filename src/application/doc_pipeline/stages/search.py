"""Стадия 2: Поиск релевантных чанков по запросу пользователя."""
from __future__ import annotations

import logging
from typing import Iterator

from application.doc_pipeline.context import DocPipelineContext
from application.doc_pipeline.events import DocPipelineEvent, SearchDone
from domain.doc_search import ChunkLocation, SearchHit
from domain.errors import CorruptedIndexError
from domain.pipeline import StageCompleted, StageStarted

log = logging.getLogger(__name__)

_REQUIRED_FIELDS = ("source_file", "start_line", "end_line", "start_offset", "end_offset")


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
            location = _parse_location(scored.document.metadata)
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


def _parse_location(meta: dict) -> ChunkLocation:
    """Валидировать метаданные и собрать ChunkLocation.

    Raises:
        CorruptedIndexError: если обязательные поля отсутствуют.
    """
    missing = [f for f in _REQUIRED_FIELDS if f not in meta]
    if missing:
        raise CorruptedIndexError(
            missing_fields=missing,
            source_file=meta.get("source_file", ""),
        )
    return ChunkLocation(
        source_file=meta["source_file"],
        start_line=meta["start_line"],
        end_line=meta["end_line"],
        start_offset=meta["start_offset"],
        end_offset=meta["end_offset"],
        section_title=meta.get("section_title", ""),
    )
