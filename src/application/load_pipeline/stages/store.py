"""Стадия 3: Сохранение — потребляет чанки, батчит, сбрасывает в ChromaDB.

Потребляет ленивый итератор ctx.chunk_results из ChunkStage.
Per-page streaming: загрузка + чанкинг происходят лениво при итерации.
В памяти: текущий батч (batch_size документов).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Iterator, Union

import chromadb.errors
from langchain_chroma import Chroma
from langchain_core.documents import Document

from domain.errors import ValidationError
from adapters.vectorstore import VectorStoreService
from application.load_pipeline.context import LoadContext
from application.load_pipeline.events import (
    StorageDone,
    StoreBatchDone,
    StoreBatchFailed,
)
from domain.pipeline import StageCompleted, StageStarted

log = logging.getLogger(__name__)

StoreStageEvent = Union[
    StageStarted, StageCompleted,
    StoreBatchDone, StoreBatchFailed, StorageDone,
]


@dataclass(frozen=True)
class BatchFlushResult:
    """Результат сохранения одного батча."""
    event: Union[StoreBatchDone, StoreBatchFailed]
    stored: int
    failed: int


class StoreStage:
    """Стадия сохранения — потребляет chunk_results, батчит, сохраняет.

    Потребляя ctx.chunk_results, лениво запускает ChunkStage → LoadPagesStage.
    Per-page streaming сохраняется.
    """

    @property
    def name(self) -> str:
        return "store"

    def run(self, ctx: LoadContext) -> Iterator[StoreStageEvent]:
        if ctx.chunk_results is None:
            raise ValidationError("chunk_results не инициализирован — ChunkStage не выполнен")

        yield StageStarted(stage=self.name)

        ctx.vectorstore_service.verify_embedding_connection()
        vectorstore = ctx.vectorstore_service.create_vectorstore(
            ctx.collection_name, ctx.embedding_model,
        )

        batch_size = ctx.storage_params.batch_size
        batch: list[Document] = []
        batch_idx = 0
        total_stored = 0
        total_failed = 0
        cumulative_chunks = 0

        for result in ctx.chunk_results:
            yield result.event  # type: ignore[misc]

            if result.chunk is None:
                continue

            cumulative_chunks = result.cumulative_chunks
            batch.append(result.chunk)

            if len(batch) >= batch_size:
                flush = _flush_batch(
                    ctx.vectorstore_service, vectorstore,
                    batch, batch_idx, total_stored, cumulative_chunks,
                )
                yield flush.event
                total_stored += flush.stored
                total_failed += flush.failed
                batch = []
                batch_idx += 1

        if batch:
            flush = _flush_batch(
                ctx.vectorstore_service, vectorstore,
                batch, batch_idx, total_stored, cumulative_chunks,
            )
            yield flush.event
            total_stored += flush.stored
            total_failed += flush.failed

        yield StorageDone(total_stored=total_stored, total_failed=total_failed)
        yield StageCompleted(stage=self.name, detail=f"сохранено {total_stored}, ошибок {total_failed}")


def _flush_batch(
    vs_service: VectorStoreService,
    vectorstore: Chroma,
    batch: list[Document],
    batch_idx: int,
    total_stored: int,
    total_chunks: int,
) -> BatchFlushResult:
    """Сохранить один батч. Возвращает результат с событием и счётчиками."""
    try:
        vs_service.store_batch(vectorstore, batch)
        return BatchFlushResult(
            event=StoreBatchDone(
                batch_index=batch_idx,
                total_stored=total_stored + len(batch),
                total_chunks=total_chunks,
            ),
            stored=len(batch),
            failed=0,
        )
    except chromadb.errors.ChromaError as e:
        log.warning("Failed to store batch %d: %s", batch_idx, e)
        return BatchFlushResult(
            event=StoreBatchFailed(
                batch_index=batch_idx,
                error=str(e),
                total_failed=len(batch),
                total_chunks=total_chunks,
            ),
            stored=0,
            failed=len(batch),
        )
