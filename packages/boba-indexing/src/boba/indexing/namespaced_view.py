"""
NamespacedView — реализация IndexQuery + IndexSink на базе фильтрации по namespace
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from itertools import islice
from typing import ClassVar, TypeVar

from boba.indexing.chunks import Chunk, ChunkSummary
from boba.indexing.content_hash import ContentHash
from boba.indexing.context import CollectionId, NamespaceId
from boba.indexing.filter import And, Eq, Filter
from boba.indexing.index_views import (
    IndexQuery,
    IndexSink,
    ReconcileSummary,
    TrackingKeys,
)
from boba.indexing.vector_store import VectorStoreReader, VectorStoreWriter

__all__ = ["NamespacedView"]

T = TypeVar("T")
_E = TypeVar("_E")


class NamespacedView(IndexQuery[T], IndexSink[T]):
    """
    Реализация: IndexQuery + IndexSink с фильтрацией chunk по namespace
    """

    NAMESPACE_KEY: ClassVar[str] = "namespace"
    DEFAULT_BATCH_SIZE: ClassVar[int] = 100

    def __init__(  # noqa: PLR0913 — честно говоря много входных, но что поделать
        self,
        store_reader: VectorStoreReader[T],
        store_writer: VectorStoreWriter[T],
        collection: CollectionId,
        namespace: NamespaceId,
        *,
        scope_extra: Filter | None = None,
        batch_size: int = DEFAULT_BATCH_SIZE,
    ) -> None:
        self._reader = store_reader
        self._writer = store_writer
        self._collection = collection
        self._namespace = namespace
        self._scope_extra = scope_extra
        self._batch_size = batch_size

    @property
    def collection(self) -> CollectionId:
        return self._collection

    @property
    def namespace(self) -> NamespaceId:
        return self._namespace

    def find(
        self,
        *,
        where: Filter | None = None,
        limit: int | None = None,
    ) -> Iterable[ChunkSummary[T]]:
        where = self._compose_filter(where)
        return self._reader.find(self._collection, where=where, limit=limit)

    def clean(self, where: Filter) -> int:
        """
        Стримит matched-summaries из find и батчами вызывает delete
        """
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
        """
        Стримит входящие chunks батчами
        """
        total = 0
        upserted = 0
        unchanged = 0

        scope_patch: dict[str, str | int | float | bool] = {
            self.NAMESPACE_KEY: self._namespace.to_wire(),
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
                scope_patch,
            )

            total += len(batch)
            upserted += len(dirty)
            unchanged += batch_unchanged

        return ReconcileSummary(
            total=total,
            upserted=upserted,
            unchanged=unchanged,
        )

    def narrow(self, where: Filter) -> NamespacedView[T]:
        new_extra: Filter = (
            And([self._scope_extra, where]) if self._scope_extra is not None else where
        )
        return NamespacedView(
            store_reader=self._reader,
            store_writer=self._writer,
            collection=self._collection,
            namespace=self._namespace,
            scope_extra=new_extra,
            batch_size=self._batch_size,
        )

    def _compose_filter(self, where: Filter | None) -> Filter:
        """
        Фильтр по namespace + custom который указал пользователь
        """
        parts: list[Filter] = [Eq(self.NAMESPACE_KEY, self._namespace.to_wire())]
        if self._scope_extra is not None:
            parts.append(self._scope_extra)

        if where is not None:
            parts.append(where)

        if len(parts) == 1:
            return parts[0]

        return And(parts)

    def _partition_dirty(
        self,
        chunks: list[Chunk[T]],
    ) -> tuple[list[Chunk[T]], int]:
        chunk_ids = [c.chunk_id for c in chunks]
        existing: dict[str, Chunk[T]] = {
            c.chunk_id.to_wire(): c
            for c in self._reader.get_by_ids(self._collection, chunk_ids)
        }
        dirty: list[Chunk[T]] = []
        unchanged_count = 0
        for c in chunks:
            stored = existing.get(c.chunk_id.to_wire())
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
        """
        Стрим делиться на батчи длиной ≤ batch_size
        """
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
