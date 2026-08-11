"""Хранение чанков: порты стора и представлений, scope-вид и стратегии очистки."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import (
    AsyncIterable,
    AsyncIterator,
    Iterable,
    Iterator,
    Mapping,
    Sequence,
)
from dataclasses import dataclass
from itertools import islice
from typing import Any, ClassVar, Generic, TypeVar

from boba.indexing.chunks import Chunk, ChunkId, ChunkSummary, EmbeddedChunk
from boba.indexing.filter import And, Filter, In, Lt
from boba.indexing.ports import Embedder
from boba.indexing.sections import SourceId
from boba.indexing.values import CollectionId, ContentHash

__all__ = [
    "ChunkStore",
    "CleanupContext",
    "CleanupStrategy",
    "CollectionInfo",
    "CollectionScopedView",
    "CollectionsStore",
    "FullCleanup",
    "HashDiff",
    "IncrementalCleanup",
    "IndexQuery",
    "IndexSink",
    "NoneCleanup",
    "ReconcileSummary",
    "TrackingKeys",
]

T = TypeVar("T")


@dataclass(frozen=True)
class HashDiff:
    """План записи после сверки по content_hash: to_upsert / unchanged.

    to_delete отсутствует намеренно — per-run cleanup устаревших чанков делает
    CleanupStrategy, не per-batch diff.
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
    async def clean(self, where: Filter) -> int:
        """Удалить чанки scope'а по фильтру, вернуть количество удалённых.

        where обязательный — предохранитель от случайной полной зачистки scope.
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
    async def reconcile(
        self,
        chunks: AsyncIterable[Chunk[T]],
        *,
        time_at_least: float,
        force: bool = False,
    ) -> ReconcileSummary:
        """Привести Store в соответствие с чанками (unchanged — только refresh
        updated_at); force=True — все dirty.
        """
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

    async def clean(self, where: Filter) -> int:
        full = self._compose_filter(where)
        deleted = 0
        summaries = await self._store.find(self._collection, where=full, limit=None)
        for batch in self._batched(summaries, self._batch_size):
            ids = [s.chunk_id for s in batch]
            await self._store.delete(self._collection, ids)
            deleted += len(ids)
        return deleted

    async def reconcile(
        self,
        chunks: AsyncIterable[Chunk[T]],
        *,
        time_at_least: float,
        force: bool = False,
    ) -> ReconcileSummary:
        """Привести Store в соответствие с chunk'ами: diff_by_hash -> embed -> upsert +
        heartbeat unchanged; force=True — весь батч dirty.
        """
        total = 0
        upserted = 0
        unchanged = 0

        refresh_patch: dict[str, str | int | float | bool] = {
            TrackingKeys.UPDATED_AT: float(time_at_least),
        }

        async for batch in self._abatched(chunks, self._batch_size):
            if force:
                changed_ids = [c.chunk_id for c in batch]
                unchanged_ids: list[ChunkId] = []
            else:
                diff = await self._store.diff_by_hash(
                    self._collection,
                    [(c.chunk_id, c.content_hash) for c in batch],
                )
                changed_ids = diff.to_upsert
                unchanged_ids = diff.unchanged

            by_id: dict[ChunkId, Chunk[T]] = {c.chunk_id: c for c in batch}
            dirty: list[Chunk[T]] = [by_id[i] for i in changed_ids]

            if dirty:
                documents = [c.format_content for c in dirty]
                embeddings = await self._embedder.embed_documents(documents)
                embedded = [
                    EmbeddedChunk.of(c, tuple(e))
                    for c, e in zip(dirty, embeddings, strict=True)
                ]
                await self._store.upsert(self._collection, embedded)

            # heartbeat только для unchanged — dirty уже получил updated_at в upsert
            if unchanged_ids:
                await self._store.update_metadata(
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


@dataclass(frozen=True)
class CleanupContext:
    """Снимок состояния одного прогона Indexer.run; query уже привязан к scope'у."""

    query: IndexQuery[Any]
    run_start: float
    touched_sources: frozenset[SourceId]


class CleanupStrategy(ABC):
    """Стратегия удаления устаревших записей в конце Indexer.run."""

    @abstractmethod
    async def execute(self, ctx: CleanupContext) -> int:
        """Выполнить cleanup; вернуть количество удалённых чанков."""
        ...


class NoneCleanup(CleanupStrategy):
    """No-op: ничего не удаляет, всегда возвращает 0."""

    async def execute(self, ctx: CleanupContext) -> int:
        del ctx
        return 0


class IncrementalCleanup(CleanupStrategy):
    """Удалить stale-записи только для touched source_id; безопасно при частичных
    прогонах.
    """

    async def execute(self, ctx: CleanupContext) -> int:
        if not ctx.touched_sources:
            return 0
        where: Filter = And(
            [
                Lt(TrackingKeys.UPDATED_AT, ctx.run_start),
                In(
                    TrackingKeys.SOURCE_ID,
                    list(ctx.touched_sources),
                ),
            ]
        )
        return await ctx.query.clean(where=where)


class FullCleanup(CleanupStrategy):
    """Удалить все stale-записи scope'а.

    Требует full-coverage от RequestSource: при частичном фиде удалит актуальные записи.
    """

    async def execute(self, ctx: CleanupContext) -> int:
        where: Filter = Lt(TrackingKeys.UPDATED_AT, ctx.run_start)
        return await ctx.query.clean(where=where)
