"""Хранение чанков: порты стора и представлений, scope-вид коллекции."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import (
    AsyncIterable,
    AsyncIterator,
    Iterable,
    Mapping,
    Sequence,
)
from dataclasses import dataclass
from typing import ClassVar, Generic, TypeVar

from boba.indexing.chunks import Chunk, ChunkId, ChunkSummary, EmbeddedChunk
from boba.indexing.filter import And, Filter
from boba.indexing.ports import Embedder
from boba.indexing.sections import SourceId
from boba.indexing.values import CollectionId, ContentHash

__all__ = [
    "ChunkStore",
    "CollectionInfo",
    "CollectionScopedView",
    "CollectionsStore",
    "HashDiff",
    "IndexQuery",
    "IndexSink",
    "ReconcileSummary",
    "TrackingKeys",
]

T = TypeVar("T")


@dataclass(frozen=True)
class HashDiff:
    """План записи после сверки по content_hash: to_upsert / unchanged.

    to_delete отсутствует намеренно: хвост чанков реиндексированного источника
    и чанки исчезнувших источников снимает IndexSink.forget по реестру.
    """

    to_upsert: list[ChunkId]
    unchanged: list[ChunkId]


@dataclass(frozen=True)
class CollectionInfo:
    """Логическая группа векторов в Store (collection в Chroma/Qdrant и т.п.)."""

    name: CollectionId
    description: str
    count: int


class ChunkStore(ABC, Generic[T]):
    """Порт хранения чанков внутри коллекции (read + write)."""

    @abstractmethod
    async def get_by_ids(
        self,
        collection: CollectionId,
        chunk_ids: Iterable[ChunkId],
    ) -> Sequence[Chunk[T]]:
        """Получить чанки по id из коллекции; пропускает несуществующие."""
        ...

    @abstractmethod
    async def peek(
        self,
        collection: CollectionId,
        *,
        source_id: SourceId | None,
        limit: int,
    ) -> Sequence[ChunkSummary[T]]:
        """Admin-просмотр: до limit ChunkSummary; source_id=None — без фильтра."""
        ...

    @abstractmethod
    async def find(
        self,
        collection: CollectionId,
        *,
        where: Filter | None,
        limit: int | None = None,
    ) -> Sequence[ChunkSummary[T]]:
        """Поиск по Filter DSL; where=None — вся коллекция, непереводимый предикат —
        UnsupportedFilterError.
        """
        ...

    @abstractmethod
    async def diff_by_hash(
        self,
        collection: CollectionId,
        candidates: Iterable[tuple[ChunkId, ContentHash]],
    ) -> HashDiff:
        """Сравнить кандидатов (chunk_id, content_hash) со Store и вернуть план записи
        HashDiff.
        """
        ...

    @abstractmethod
    async def upsert(
        self,
        collection: CollectionId,
        chunks: Iterable[EmbeddedChunk[T]],
    ) -> None:
        """Bulk-upsert EmbeddedChunk[T]: полная замена записи по chunk_id, включая
        удаление отсутствующих metadata-ключей.
        """
        ...

    @abstractmethod
    async def update_metadata(
        self,
        collection: CollectionId,
        chunk_ids: Iterable[ChunkId],
        patch: Mapping[str, str | int | float | bool],
    ) -> None:
        """Patch-обновление только перечисленных metadata-ключей без re-embed."""
        ...

    @abstractmethod
    async def delete(
        self,
        collection: CollectionId,
        chunk_ids: Iterable[ChunkId],
    ) -> None:
        """Удалить чанки по id из коллекции; несуществующие игнорируются."""
        ...

    @abstractmethod
    async def delete_by_source(
        self,
        collection: CollectionId,
        source_id: SourceId,
        *,
        from_index: int,
    ) -> int:
        """Удалить чанки источника с chunk_index >= from_index; вернуть число."""
        ...


class CollectionsStore(ABC):
    """Read-side admin: перечисление и инспекция коллекций."""

    @abstractmethod
    async def list_collections(self) -> Sequence[CollectionInfo]:
        """Все коллекции в backend'е."""
        ...

    @abstractmethod
    async def collection_info(self, name: CollectionId) -> CollectionInfo:
        """Сводка одной коллекции по имени."""
        ...

    @abstractmethod
    async def ensure_collection(
        self,
        name: CollectionId,
        *,
        description: str | None,
    ) -> None:
        """Создать коллекцию name, если отсутствует. Idempotent."""
        ...

    @abstractmethod
    async def delete_collection(self, name: CollectionId) -> None:
        """Удалить коллекцию целиком."""
        ...


T = TypeVar("T")


class TrackingKeys:
    """Wire-имена tracking-полей в metadata-store — единый источник правды для всех
    backend'ов.
    """

    CONTENT_HASH: ClassVar[str] = "content_hash"
    UPDATED_AT: ClassVar[str] = "updated_at"
    SOURCE_ID: ClassVar[str] = "source_id"
    CHUNK_INDEX: ClassVar[str] = "chunk_index"
    TAGS: ClassVar[str] = "tags"


@dataclass(frozen=True)
class ReconcileSummary:
    """Результат IndexSink.reconcile: total / upserted / unchanged."""

    total: int
    upserted: int
    unchanged: int


class IndexQuery(ABC, Generic[T]):
    """Filter-based view: реализация инжектит scope-фильтр в каждый запрос, чужой scope
    недостижим.
    """

    @abstractmethod
    async def find(
        self,
        *,
        where: Filter | None = None,
        limit: int | None = None,
    ) -> Sequence[ChunkSummary[T]]:
        """Scope-aware поиск по фильтру; where=None — только scope-фильтр, limit=None —
        без лимита.
        """
        ...

    @abstractmethod
    def narrow(self, where: Filter) -> IndexQuery[T]:
        """Новый IndexQuery с добавленным Filter; каскад narrow(a).narrow(b) ≡
        narrow(And([a, b])).
        """
        ...


class IndexSink(ABC, Generic[T]):
    """Запись chunk'ов через reconcile с идемпотентной проверкой по content_hash."""

    @abstractmethod
    async def reconcile(self, chunks: AsyncIterable[Chunk[T]]) -> ReconcileSummary:
        """Записать изменившиеся чанки; совпавшие по content_hash не трогаются."""
        ...

    @abstractmethod
    async def forget(self, source_id: SourceId, *, from_index: int) -> int:
        """Снять чанки источника начиная с from_index; вернуть число удалённых."""
        ...


T = TypeVar("T")
_E = TypeVar("_E")


class CollectionScopedView(IndexQuery[T], IndexSink[T]):
    """IndexQuery + IndexSink со scope'ом в одну collection Store; сужение — через
    narrow(...).
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

    async def find(
        self,
        *,
        where: Filter | None = None,
        limit: int | None = None,
    ) -> Sequence[ChunkSummary[T]]:
        composed = self._compose_filter(where)
        return await self._store.find(self._collection, where=composed, limit=limit)

    async def reconcile(self, chunks: AsyncIterable[Chunk[T]]) -> ReconcileSummary:
        """Батчами: diff_by_hash -> embed -> upsert изменившихся."""
        total = 0
        upserted = 0
        unchanged = 0

        async for batch in self._abatched(chunks, self._batch_size):
            candidates: list[tuple[ChunkId, ContentHash]] = []
            for chunk in batch:
                candidates.append((chunk.chunk_id, chunk.content_hash))

            diff = await self._store.diff_by_hash(self._collection, candidates)

            by_id: dict[ChunkId, Chunk[T]] = {}
            for chunk in batch:
                by_id[chunk.chunk_id] = chunk

            dirty: list[Chunk[T]] = []
            for chunk_id in diff.to_upsert:
                dirty.append(by_id[chunk_id])

            if dirty:
                await self._upsert(dirty)

            total += len(batch)
            upserted += len(dirty)
            unchanged += len(diff.unchanged)

        return ReconcileSummary(
            total=total,
            upserted=upserted,
            unchanged=unchanged,
        )

    async def _upsert(self, dirty: Sequence[Chunk[T]]) -> None:
        documents: list[T] = []
        for chunk in dirty:
            documents.append(chunk.format_content)

        embeddings = await self._embedder.embed_documents(documents)

        embedded: list[EmbeddedChunk[T]] = []
        for chunk, vector in zip(dirty, embeddings, strict=True):
            embedded.append(EmbeddedChunk.of(chunk, tuple(vector)))

        await self._store.upsert(self._collection, embedded)

    async def forget(self, source_id: SourceId, *, from_index: int) -> int:
        return await self._store.delete_by_source(
            self._collection,
            source_id,
            from_index=from_index,
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
    async def _abatched(
        items: AsyncIterable[_E],
        batch_size: int,
    ) -> AsyncIterator[list[_E]]:
        batch: list[_E] = []
        async for item in items:
            batch.append(item)
            if len(batch) >= batch_size:
                yield batch
                batch = []
        if batch:
            yield batch
