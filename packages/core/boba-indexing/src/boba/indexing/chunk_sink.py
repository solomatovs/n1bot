"""
ChunkSink[T] - потребитель EmbeddedChunk[T] в pipeline
кладёт чанки в backend (с возможной буферизацией) и flush по команде

Отделяет pipeline-лимиты (batching, transactional flush) от чистой логики
хранения ChunkStore[T].

Зачем отдельная абстракция? Несколько причин:

1. Заменяемая терминальная стадия:
    - VectorStoreChunkSink - делегирует raw батч-upsert в ChunkStore[T]
      (без idempotency-check'а; это «голая запись», не reconcile)
    - LoggingChunkSink - логирует чанки вместо сохранения (для отладки)
    - MultiChunkSink - рассылает чанки в несколько других ChunkSink (fan-out)
    - MetricsChunkSink - собирает статистику по чанкам, не сохраняя их

2. Логика batching и flush — не загрязняют ChunkStore

3. Тестирование: легко подсунуть InMemoryChunkSink, не реализуя весь ChunkStore

ChunkSink — НЕ замена IndexSink: IndexSink делает reconcile (idempotency +
upsert + refresh updated_at), а ChunkSink — просто batched raw upsert.
Используются для разных целей. Pipeline indexing идёт через IndexSink;
ChunkSink — для специфических case'ов вроде fan-out на несколько backend'ов
или log-only debug-трасс.

ChunkSink работает с `EmbeddedChunk[T]` (chunk + готовый embedding):
embedder в Store не инжектится, его дёргает caller (или верхний sink-слой)
перед `handle`.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable
from typing import Generic, TypeVar

from boba.indexing.chunk_store import ChunkStore
from boba.indexing.chunks import EmbeddedChunk
from boba.indexing.context import CollectionId, PipelineContext
from boba.patterns import StreamSink

__all__ = ["ChunkSink", "VectorStoreChunkSink"]

T = TypeVar("T")


class ChunkSink(StreamSink[PipelineContext, EmbeddedChunk[T]], ABC, Generic[T]):
    """
    Терминальный потребитель `EmbeddedChunk[T]` в pipeline.

    `handle(ctx, chunk)` — кладёт чанк в backend (с возможной буферизацией)
    `flush(ctx)` — сбрасывает буфер; обязан вызываться pipeline в конце
    """

    @abstractmethod
    def flush(self, ctx: PipelineContext) -> None:
        """Сбросить накопленный буфер в backend."""
        ...


class VectorStoreChunkSink(ChunkSink[T], Generic[T]):
    """
    ChunkSink, который батчит `EmbeddedChunk[T]` до `batch_size` и
    автоматически flush'ит их через `ChunkStore.upsert(collection, batch)`.
    Collection связан с sink'ом при конструировании — каждая
    VectorStoreChunkSink-инстанция пишет в одну фиксированную коллекцию.

    `flush(ctx)` вручную — для остатка буфера в конце прогона.

    Использует raw `ChunkStore.upsert` без idempotency-check'а; для
    нормальной indexing-стороны лучше брать `IndexSink.reconcile`.
    """

    def __init__(
        self,
        store: ChunkStore[T],
        collection: CollectionId,
        batch_size: int = 100,
    ) -> None:
        if batch_size < 1:
            raise ValueError(f"batch_size must be >= 1, got {batch_size}")

        self._store = store
        self._collection = collection
        self._batch_size = batch_size
        self._buffer: list[EmbeddedChunk[T]] = []

    def name(self) -> str:
        return f"VectorStoreChunkSink(batch={self._batch_size})"

    def handle(self, ctx: PipelineContext, event: EmbeddedChunk[T]) -> None:
        del ctx
        self._buffer.append(event)
        if len(self._buffer) >= self._batch_size:
            self._flush_batch(self._buffer)
            self._buffer = []

    def flush(self, ctx: PipelineContext) -> None:
        del ctx
        if self._buffer:
            self._flush_batch(self._buffer)
            self._buffer = []

    def _flush_batch(self, batch: Iterable[EmbeddedChunk[T]]) -> None:
        self._store.upsert(self._collection, batch)
