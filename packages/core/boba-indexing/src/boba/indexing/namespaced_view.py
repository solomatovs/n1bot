"""
NamespacedView — реализация IndexQuery + IndexSink на базе фильтрации по namespace
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from itertools import islice
from typing import ClassVar, TypeVar

from boba.indexing.chunks import Chunk, ChunkId, ChunkSummary, EmbeddedChunk
from boba.indexing.context import CollectionId, NamespaceId
from boba.indexing.embedder import Embedder
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
        embedder: Embedder[T],
        collection: CollectionId,
        namespace: NamespaceId,
        *,
        scope_extra: Filter | None = None,
        batch_size: int = DEFAULT_BATCH_SIZE,
    ) -> None:
        self._reader = store_reader
        self._writer = store_writer
        self._embedder = embedder
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
        Привести Store в соответствие с пришедшими chunk'ами.

        Per-batch:
          1. `diff_by_hash` (один SELECT) делит батч на to_upsert/unchanged
          2. embedder только на to_upsert
          3. upsert(EmbeddedChunk) пишет dirty + ставит updated_at=now() и
             scope-tag namespace в metadata
          4. update_metadata(scope_patch) для всех — нужен и для dirty,
             потому что namespace-тэг инжектится здесь и должен прописаться
             даже на свежевставленных чанках (в metadata через merge)

        `force=True` обходит diff и треатит весь батч как dirty.
        """
        total = 0
        upserted = 0
        unchanged = 0

        scope_patch: dict[str, str | int | float | bool] = {
            self.NAMESPACE_KEY: self._namespace,
            TrackingKeys.UPDATED_AT: float(time_at_least),
        }

        for batch in self._batched(chunks, self._batch_size):
            if force:
                to_upsert_ids = [c.chunk_id for c in batch]
                unchanged_ids: list[ChunkId] = []
            else:
                diff = self._reader.diff_by_hash(
                    self._collection,
                    [(c.chunk_id, c.content_hash) for c in batch],
                )
                to_upsert_ids = diff.to_upsert
                unchanged_ids = diff.unchanged

            by_id: dict[ChunkId, Chunk[T]] = {c.chunk_id: c for c in batch}
            dirty: list[Chunk[T]] = [by_id[i] for i in to_upsert_ids]

            if dirty:
                embeddings = list(
                    self._embedder.embed_documents(
                        [c.format_content for c in dirty]
                    )
                )

                embedded = [
                    EmbeddedChunk.of(c, tuple(e))
                    for c, e in zip(dirty, embeddings, strict=True)
                ]
                self._writer.upsert(self._collection, embedded)

            # namespace-тэг инжектится здесь — ему нужно прописаться и для
            # dirty (upsert метадату затирает целиком, но namespace в DTO
            # не входит — это property view'а, не Chunk'а), и для unchanged
            # (вместе с heartbeat updated_at).
            self._writer.update_metadata(
                self._collection,
                [c.chunk_id for c in batch],
                scope_patch,
            )

            total += len(batch)
            upserted += len(dirty)
            unchanged += len(unchanged_ids)

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
            embedder=self._embedder,
            collection=self._collection,
            namespace=self._namespace,
            scope_extra=new_extra,
            batch_size=self._batch_size,
        )

    def _compose_filter(self, where: Filter | None) -> Filter:
        """
        Фильтр по namespace + custom который указал пользователь
        """
        parts: list[Filter] = [Eq(self.NAMESPACE_KEY, self._namespace)]
        if self._scope_extra is not None:
            parts.append(self._scope_extra)

        if where is not None:
            parts.append(where)

        if len(parts) == 1:
            return parts[0]

        return And(parts)

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
