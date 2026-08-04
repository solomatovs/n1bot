"""Абстракции хранения чанков: ChunkStore[T] (внутри коллекции) и CollectionsStore (CRUD коллекций)."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Generic, TypeVar

from boba.indexing.chunks import Chunk, ChunkId, ChunkSummary, EmbeddedChunk
from boba.indexing.content_hash import ContentHash
from boba.indexing.context import CollectionId
from boba.indexing.filter import Filter
from boba.indexing.sections import SourceId

__all__ = [
    "ChunkStore",
    "CollectionInfo",
    "CollectionsStore",
    "HashDiff",
]

T = TypeVar("T")


@dataclass(frozen=True)
class HashDiff:
    """План записи после сверки по content_hash: to_upsert / unchanged.

    to_delete отсутствует намеренно — per-run cleanup устаревших чанков делает CleanupStrategy, не per-batch diff.
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
        """Поиск по Filter DSL; where=None — вся коллекция, непереводимый предикат — UnsupportedFilterError."""
        ...

    @abstractmethod
    async def diff_by_hash(
        self,
        collection: CollectionId,
        candidates: Iterable[tuple[ChunkId, ContentHash]],
    ) -> HashDiff:
        """Сравнить кандидатов (chunk_id, content_hash) со Store и вернуть план записи HashDiff."""
        ...

    @abstractmethod
    async def upsert(
        self,
        collection: CollectionId,
        chunks: Iterable[EmbeddedChunk[T]],
    ) -> None:
        """Bulk-upsert EmbeddedChunk[T]: полная замена записи по chunk_id, включая удаление отсутствующих metadata-ключей."""
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
