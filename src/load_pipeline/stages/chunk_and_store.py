"""Стадия 2: Потоковая загрузка → чанкинг → сохранение в ChromaDB.

Потребляет ленивый итератор загрузки из контекста.
Обрабатывает постранично: загрузил страницу → чанки → в батч → сбросил.
В памяти только текущая страница + текущий батч.
Все счётчики — локальные, не протекают в контекст.
"""
from __future__ import annotations

import logging
from typing import Generator, Iterator, Tuple, Union

import chromadb.errors
from langchain_chroma import Chroma
from langchain_core.documents import Document

from pipeline.events import StageCompleted, StageStarted
from load_pipeline.context import LoadContext, LoadEvent
from load_pipeline.events import (
    ChunkingDone,
    ChunkProduced,
    SectionChunked,
    PageLoaded,
    StorageDone,
    StoreBatchDone,
    StoreBatchFailed,
)
from vectorstore import VectorStoreService

log = logging.getLogger(__name__)

ChunkAndStoreEvent = Union[
    StageStarted, StageCompleted,
    LoadEvent,
    SectionChunked, ChunkProduced, ChunkingDone,
    StoreBatchDone, StoreBatchFailed, StorageDone,
]


class ChunkAndStoreStage:
    """Потоковый чанкинг + батчевое сохранение с per-page streaming.

    Потребляет ленивый итератор загрузки из ctx.loading_events.
    На каждую загруженную страницу: чанкинг → накопление батча → сброс в store.
    """

    @property
    def name(self) -> str:
        return "chunk_and_store"

    def run(self, ctx: LoadContext) -> Iterator[ChunkAndStoreEvent]:
        yield StageStarted(stage=self.name)

        assert ctx.loading_events is not None

        ctx.vectorstore_service.verify_embedding_connection()
        vectorstore = ctx.vectorstore_service.get_vectorstore(ctx.collection_name)

        yield from _stream_chunk_and_store(ctx, vectorstore)

        yield StageCompleted(stage=self.name, detail="загрузка завершена")


def _stream_chunk_and_store(
    ctx: LoadContext,
    vectorstore: Chroma,
) -> Iterator[ChunkAndStoreEvent]:
    """Основной streaming-цикл: загрузка → чанкинг → батчинг → сброс.

    Все счётчики — локальные переменные. В памяти: текущая страница + батч.
    """
    assert ctx.loading_events is not None

    batch_size = ctx.storage_params.batch_size
    batch: list[Document] = []
    batch_idx = 0
    total_stored = 0
    total_failed = 0
    cumulative_chunks = 0
    has_documents = False

    for event in ctx.loading_events:
        yield event

        if not isinstance(event, PageLoaded):
            continue

        if not event.documents:
            continue

        has_documents = True

        for chunk_event in ctx.chunker.split_documents(event.documents):
            if isinstance(chunk_event, SectionChunked):
                yield SectionChunked(
                    doc_index=event.index,
                    doc_total=event.total,
                    section_index=chunk_event.section_index,
                    section_total=chunk_event.section_total,
                    section_title=chunk_event.section_title,
                )
            elif isinstance(chunk_event, ChunkProduced):
                cumulative_chunks += 1
                yield ChunkProduced(
                    chunk=chunk_event.chunk,
                    cumulative_chunks=cumulative_chunks,
                )
                batch.append(chunk_event.chunk)

                if len(batch) >= batch_size:
                    stored, failed = yield from _flush_batch(
                        ctx.vectorstore_service, vectorstore,
                        batch, batch_idx, total_stored, cumulative_chunks,
                    )
                    total_stored += stored
                    total_failed += failed
                    batch = []
                    batch_idx += 1

    # Дочищаем остаток батча
    if batch:
        stored, failed = yield from _flush_batch(
            ctx.vectorstore_service, vectorstore,
            batch, batch_idx, total_stored, cumulative_chunks,
        )
        total_stored += stored
        total_failed += failed

    if has_documents and cumulative_chunks > 0:
        yield ChunkingDone(total_chunks=cumulative_chunks)

    yield StorageDone(total_stored=total_stored, total_failed=total_failed)


def _flush_batch(
    vs_service: VectorStoreService,
    vectorstore: Chroma,
    batch: list[Document],
    batch_idx: int,
    total_stored: int,
    total_chunks: int,
) -> Generator[Union[StoreBatchDone, StoreBatchFailed], None, Tuple[int, int]]:
    """Сохранить один батч, yield результат. Возвращает (stored, failed)."""
    try:
        vs_service.store_batch(vectorstore, batch)
        yield StoreBatchDone(
            batch_index=batch_idx,
            total_stored=total_stored + len(batch),
            total_chunks=total_chunks,
        )
        return len(batch), 0
    except chromadb.errors.ChromaError as e:
        log.warning("Батч %d не удалось сохранить: %s", batch_idx, e)
        yield StoreBatchFailed(
            batch_index=batch_idx,
            error=str(e),
            total_failed=len(batch),
            total_chunks=total_chunks,
        )
        return 0, len(batch)
