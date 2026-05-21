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

from boba.indexing.chunks import Chunk, ChunkSummary
from boba.indexing.content_hash import ContentHash
from boba.indexing.context import CollectionId
from boba.indexing.filter import And, Filter
from boba.indexing.index_views import (
    IndexQuery,
    IndexSink,
    ReconcileSummary,
    TrackingKeys,
)
from boba.indexing.vector_store import VectorStoreReader, VectorStoreWriter

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
        store_reader: VectorStoreReader[T],
        store_writer: VectorStoreWriter[T],
        collection: CollectionId,
        *,
        scope_extra: Filter | None = None,
        batch_size: int = DEFAULT_BATCH_SIZE,
    ) -> None:
        self._reader = store_reader
        self._writer = store_writer
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
        return self._reader.find(self._collection, where=composed, limit=limit)

    def clean(self, where: Filter) -> int:
        full = self._compose_filter(where)
        deleted = 0
        summaries = self._reader.find(self._collection, where=full, limit=None)
        for batch in self._batched(summaries, self._batch_size):
            ids = [s.chunk_id for s in batch]
            self._writer.delete(self._collection, ids)
            deleted += len(ids)
        return deleted

    def reconcile(
        self,
        chunks: Iterable[Chunk[T]],
        *,
        time_at_least: float,
        force: bool = False,
    ) -> ReconcileSummary:
        total = 0
        upserted = 0
        unchanged = 0

        refresh_patch: dict[str, str | int | float | bool] = {
            TrackingKeys.UPDATED_AT: float(time_at_least),
        }

        for batch in self._batched(chunks, self._batch_size):
            if force:
                dirty = batch
                batch_unchanged = 0
            else:
                dirty, batch_unchanged = self._partition_dirty(batch)

            if dirty:
                self._writer.upsert(self._collection, dirty)

            self._writer.update_metadata(
                self._collection,
                [c.chunk_id for c in batch],
                refresh_patch,
            )

            total += len(batch)
            upserted += len(dirty)
            unchanged += batch_unchanged

        return ReconcileSummary(
            total=total,
            upserted=upserted,
            unchanged=unchanged,
        )

    def narrow(self, where: Filter) -> CollectionScopedView[T]:
        new_extra: Filter = (
            And([self._scope_extra, where])
            if self._scope_extra is not None
            else where
        )
        return CollectionScopedView(
            store_reader=self._reader,
            store_writer=self._writer,
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

    def _partition_dirty(
        self,
        chunks: list[Chunk[T]],
    ) -> tuple[list[Chunk[T]], int]:
        chunk_ids = [c.chunk_id for c in chunks]
        existing: dict[str, Chunk[T]] = {
            c.chunk_id: c
            for c in self._reader.get_by_ids(self._collection, chunk_ids)
        }
        dirty: list[Chunk[T]] = []
        unchanged_count = 0
        for c in chunks:
            stored = existing.get(c.chunk_id)
            if stored is None:
                dirty.append(c)
                continue
            if self._hashes_equal(stored.content_hash, c.content_hash):
                unchanged_count += 1
            else:
                dirty.append(c)
        return dirty, unchanged_count

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

    @staticmethod
    def _hashes_equal(
        a: ContentHash | None,
        b: ContentHash | None,
    ) -> bool:
        if a is None or b is None:
            return a is None and b is None
        return a.to_wire() == b.to_wire()
