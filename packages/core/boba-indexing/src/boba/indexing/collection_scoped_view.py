"""
CollectionScopedView — реализация IndexQuery + IndexSink без metadata-фильтра.

В отличие от NamespacedView, не инжектит metadata-предикат
(`namespace=...`) в каждый запрос. Полагается на то, что сам Store
разделяет данные по `collection` нативно (отдельная колонка в таблице,
отдельный keyspace, и т.п.) — это и есть единственный scope view'а.

Соответствует термину `GlobalView(store, collection)` из docstring
`boba.indexing.index_views` — view без scope-фильтра поверх collection.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from itertools import islice
from typing import ClassVar, TypeVar

from boba.indexing.chunk_store import ChunkStore
from boba.indexing.chunks import Chunk, ChunkId, ChunkSummary, EmbeddedChunk
from boba.indexing.context import CollectionId
from boba.indexing.embedder import Embedder
from boba.indexing.filter import And, Filter
from boba.indexing.index_views import (
    IndexQuery,
    IndexSink,
    ReconcileSummary,
    TrackingKeys,
)

__all__ = ["CollectionScopedView"]

T = TypeVar("T")
_E = TypeVar("_E")


class CollectionScopedView(IndexQuery[T], IndexSink[T]):
    """
    IndexQuery + IndexSink, scope которого равен ровно одной `collection`
    у Store. Никаких metadata-фильтров поверх не добавляется.

    Подходит для backend'ов, где collection — first-class поле хранения
    (например pgvector с колонкой `collection` в kb_chunks), а namespace
    разделение либо не нужно, либо моделируется через `narrow(...)`.
    """

    DEFAULT_BATCH_SIZE: ClassVar[int] = 100

    def __init__(
        self,
        store: ChunkStore[T],
        embedder: Embedder[T],
        collection: CollectionId,
        *,
        scope_extra: Filter | None = None,
        batch_size: int = DEFAULT_BATCH_SIZE,
    ) -> None:
        self._store = store
        self._embedder = embedder
        self._collection = collection
        self._scope_extra = scope_extra
        self._batch_size = batch_size

    @property
    def collection(self) -> CollectionId:
        return self._collection

    def find(
        self,
        *,
        where: Filter | None = None,
        limit: int | None = None,
    ) -> Iterable[ChunkSummary[T]]:
        composed = self._compose_filter(where)
        return self._store.find(self._collection, where=composed, limit=limit)

    def clean(self, where: Filter) -> int:
        full = self._compose_filter(where)
        deleted = 0
        summaries = self._store.find(self._collection, where=full, limit=None)
        for batch in self._batched(summaries, self._batch_size):
            ids = [s.chunk_id for s in batch]
            self._store.delete(self._collection, ids)
            deleted += len(ids)
        return deleted

    def reconcile(
        self,
        chunks: Iterable[Chunk[T]],
        *,
        time_at_least: float,
        force: bool = False,
    ) -> ReconcileSummary:
        """
        Привести Store в соответствие с пришедшими chunk'ами.

        Per-batch flow:
          1. `diff_by_hash` — один SELECT по (chunk_id, content_hash),
             разделяет батч на to_upsert/unchanged
          2. embedder.embed_documents — только для to_upsert
          3. writer.upsert — пишет dirty с уже посчитанным embedding'ом;
             `updated_at` в той же INSERT/UPDATE
          4. writer.update_metadata — heartbeat для unchanged
             (нужно для IncrementalCleanup/FullCleanup, иначе их сметёт)

        `force=True` обходит diff и трактует весь батч как dirty.
        """
        total = 0
        upserted = 0
        unchanged = 0

        # время pipeline обновления для FullCleanup/IncrementalCleanup
        # стратегий (boba/indexing/cleanup.py).
        refresh_patch: dict[str, str | int | float | bool] = {
            TrackingKeys.UPDATED_AT: float(time_at_least),
        }

        for batch in self._batched(chunks, self._batch_size):
            if force:
                changed_ids = [c.chunk_id for c in batch]
                unchanged_ids: list[ChunkId] = []
            else:
                diff = self._store.diff_by_hash(
                    self._collection,
                    [(c.chunk_id, c.content_hash) for c in batch],
                )
                changed_ids = diff.to_upsert
                unchanged_ids = diff.unchanged

            by_id: dict[ChunkId, Chunk[T]] = {c.chunk_id: c for c in batch}
            dirty: list[Chunk[T]] = [by_id[i] for i in changed_ids]

            if dirty:
                documents = [c.format_content for c in dirty]
                embeddings = list(self._embedder.embed_documents(documents))
                embedded = [
                    EmbeddedChunk.of(c, tuple(e))
                    for c, e in zip(dirty, embeddings, strict=True)
                ]
                self._store.upsert(self._collection, embedded)

            # heartbeat только для unchanged — для dirty `updated_at = now()`
            # уже выставил upsert в INSERT ... ON CONFLICT.
            if unchanged_ids:
                self._store.update_metadata(
                    self._collection,
                    unchanged_ids,
                    refresh_patch,
                )

            total += len(batch)
            upserted += len(dirty)
            unchanged += len(unchanged_ids)

        return ReconcileSummary(
            total=total,
            upserted=upserted,
            unchanged=unchanged,
        )

    def narrow(self, where: Filter) -> CollectionScopedView[T]:
        new_extra: Filter = (
            And([self._scope_extra, where]) if self._scope_extra is not None else where
        )
        return CollectionScopedView(
            store=self._store,
            embedder=self._embedder,
            collection=self._collection,
            scope_extra=new_extra,
            batch_size=self._batch_size,
        )

    def _compose_filter(self, where: Filter | None) -> Filter | None:
        parts: list[Filter] = []
        if self._scope_extra is not None:
            parts.append(self._scope_extra)
        if where is not None:
            parts.append(where)

        if not parts:
            return None
        if len(parts) == 1:
            return parts[0]
        return And(parts)

    @staticmethod
    def _batched(
        items: Iterable[_E],
        batch_size: int,
    ) -> Iterator[list[_E]]:
        it = iter(items)
        while True:
            batch = list(islice(it, batch_size))
            if not batch:
                return
            yield batch
