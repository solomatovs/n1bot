"""Стадия 2: Чанкинг — потребляет загруженные страницы, yield-ит чанки.

Обогащает внутренние события чанкера (SectionProcessed, ChunkCreated)
контекстом страницы (doc_index, doc_total, cumulative_chunks)
и преобразует во внешние события (SectionChunked, ChunkProduced).

Записывает в ctx.chunk_results ленивый итератор ChunkResult
для потребления стадией StoreStage.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterator, Optional, Union

from langchain_core.documents import Document

from errors import ValidationError
from load_pipeline.chunking import ChunkCreated, ChunkingStrategy, SectionProcessed
from load_pipeline.events import (
    ChunkingDone,
    ChunkProduced,
    PageLoaded,
    SectionChunked,
)
from load_pipeline.context import LoadContext, LoadEvent
from pipeline.events import StageCompleted, StageStarted

ChunkStageEvent = Union[StageStarted, StageCompleted]
ChunkEvent = Union[SectionChunked, ChunkProduced, ChunkingDone, LoadEvent]


@dataclass(frozen=True)
class ChunkResult:
    """Один элемент потока чанкинга — событие + опциональный Document для сохранения."""
    event: ChunkEvent
    chunk: Optional[Document] = None
    cumulative_chunks: int = 0


class ChunkStage:
    """Стадия чанкинга — создаёт ленивый итератор ChunkResult в контексте.

    Не потребляет loading_events сама — создаёт генератор,
    который StoreStage потребит лениво (per-page streaming).
    """

    @property
    def name(self) -> str:
        return "chunk"

    def run(self, ctx: LoadContext) -> Iterator[ChunkStageEvent]:
        if ctx.loading_events is None:
            raise ValidationError("loading_events не инициализирован — LoadPagesStage не выполнен")

        yield StageStarted(stage=self.name)

        ctx.chunk_results = _create_chunk_stream(ctx.loading_events, ctx.chunker)

        yield StageCompleted(stage=self.name, detail="итератор чанкинга готов")


def _create_chunk_stream(
    loading_events: Iterator[LoadEvent],
    chunker: ChunkingStrategy,
) -> Iterator[ChunkResult]:
    """Ленивый генератор: потребляет загрузку, чанкит, yield-ит ChunkResult.

    Обогащает внутренние события чанкера контекстом страницы:
    - SectionProcessed → SectionChunked (добавляет doc_index, doc_total)
    - ChunkCreated → ChunkProduced (добавляет cumulative_chunks)
    """
    cumulative = 0

    for event in loading_events:
        yield ChunkResult(event=event)

        if not isinstance(event, PageLoaded):
            continue
        if not event.documents:
            continue

        for chunker_event in chunker.split_documents(event.documents):
            if isinstance(chunker_event, SectionProcessed):
                yield ChunkResult(
                    event=SectionChunked(
                        doc_index=event.index,
                        doc_total=event.total,
                        section_index=chunker_event.section_index,
                        section_total=chunker_event.section_total,
                        section_title=chunker_event.section_title,
                    ),
                )
            elif isinstance(chunker_event, ChunkCreated):
                cumulative += 1
                yield ChunkResult(
                    event=ChunkProduced(chunk=chunker_event.chunk, cumulative_chunks=cumulative),
                    chunk=chunker_event.chunk,
                    cumulative_chunks=cumulative,
                )

    if cumulative > 0:
        yield ChunkResult(event=ChunkingDone(total_chunks=cumulative))
