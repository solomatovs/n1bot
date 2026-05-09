"""
ChunkSink[T] - потребитель Chunk[T] в pipeline
кладёт чанки в backend (с возможной буферизацией) и flush по команде

Отделяет pipeline-лимиты (per-call ctx, batching, transactional flush)
от чистой логики хранения VectorStore[T]

Зачем отдельная абстракция? Несколько причин:

1. заменяемая терминальная стадия к примеру такие:
    - VectorStoreChunkSink - делегирует батч-upsert в VectorStoreWriter[T]
    - LoggingChunkSink - логирует чанки вместо сохранения (для отладки)
    - MultiChunkSink - рассылает чанки в несколько других ChunkSink (для fan-out)
    - MetricsChunkSink - собирает статистику по чанкам, не сохраняя их (для мониторинга)

2. логика batching и flush — не загрязняют VectorStore

3. тестирование: легко подсунуть InMemoryChunkSink, не реализуя весь VectorStore
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable
from typing import Generic, TypeVar

from boba.indexing.chunks import Chunk
from boba.indexing.context import CollectionId, PipelineContext
from boba.indexing.vector_store import VectorStoreWriter
from boba.patterns import StreamSink

__all__ = ["ChunkSink", "VectorStoreChunkSink"]

T = TypeVar("T")


class ChunkSink(StreamSink[PipelineContext, Chunk[T]], ABC, Generic[T]):
    """
    Терминальный потребитель Chunk[T] в pipeline

    `handle(ctx, chunk)` — кладёт чанк в backend (с возможной буферизацией)
    `flush(ctx)` — сбрасывает буфер; обязан вызываться pipeline в конце
    """

    @abstractmethod
    def flush(self, ctx: PipelineContext) -> None:
        """Сбросить накопленный буфер в backend для коллекции `ctx.collection`."""
        ...


class VectorStoreChunkSink(ChunkSink[T], Generic[T]):
    """
    ChunkSink - который батчит чанки до `batch_size`
    после чего автоматически flush в `store.upsert(ctx.collection, batch)`
    `flush(ctx)` вручную — для остатка буфера в конце прогона
    """

    def __init__(
        self,
        store: VectorStoreWriter[T],
        batch_size: int = 100,
    ) -> None:
        if batch_size < 1:
            raise ValueError(f"batch_size must be >= 1, got {batch_size}")

        self._store = store
        self._batch_size = batch_size
        self._buffer: list[Chunk[T]] = []

    def name(self) -> str:
        return f"VectorStoreChunkSink(batch={self._batch_size})"

    def handle(self, ctx: PipelineContext, event: Chunk[T]) -> None:
        self._buffer.append(event)
        if len(self._buffer) >= self._batch_size:
            self._flush_batch(ctx.collection, self._buffer)
            self._buffer = []

    def flush(self, ctx: PipelineContext) -> None:
        if self._buffer:
            self._flush_batch(ctx.collection, self._buffer)
            self._buffer = []

    def _flush_batch(
        self, collection: CollectionId, batch: Iterable[Chunk[T]]
    ) -> None:
        self._store.upsert(collection, batch)
