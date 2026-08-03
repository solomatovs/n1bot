"""Абстракции хранения чанков: ChunkStore[T] (внутри коллекции) и CollectionsStore (CRUD коллекций)."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable, Mapping
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
    def get_by_ids(
        self,
        collection: CollectionId,
        chunk_ids: Iterable[ChunkId],
    ) -> Iterable[Chunk[T]]:
        """Получить чанки по id из коллекции; пропускает несуществующие."""
        ...

    @abstractmethod
    def peek(
        self,
        collection: CollectionId,
        *,
        source_id: SourceId | None,
        limit: int,
    ) -> Iterable[ChunkSummary[T]]:
        """Admin-просмотр: до limit ChunkSummary; source_id=None — без фильтра."""
        ...

    @abstractmethod
    def find(
        self,
        collection: CollectionId,
        *,
        where: Filter | None,
        limit: int | None = None,
    ) -> Iterable[ChunkSummary[T]]:
        """Поиск по Filter DSL; where=None — вся коллекция, непереводимый предикат — UnsupportedFilterError."""
        ...

    @abstractmethod
    def diff_by_hash(
        self,
        collection: CollectionId,
        candidates: Iterable[tuple[ChunkId, ContentHash]],
    ) -> HashDiff:
        """Сравнить кандидатов (chunk_id, content_hash) со Store и вернуть план записи HashDiff."""
        ...

    @abstractmethod
    def upsert(
        self,
        collection: CollectionId,
        chunks: Iterable[EmbeddedChunk[T]],
    ) -> None:
        """Bulk-upsert EmbeddedChunk[T]: полная замена записи по chunk_id, включая удаление отсутствующих metadata-ключей."""
        ...

    @abstractmethod
    def update_metadata(
        self,
        collection: CollectionId,
        chunk_ids: Iterable[ChunkId],
        patch: Mapping[str, str | int | float | bool],
    ) -> None:
        """Patch-обновление только перечисленных metadata-ключей без re-embed."""
        ...

    @abstractmethod
    def delete(
        self,
        collection: CollectionId,
        chunk_ids: Iterable[ChunkId],
    ) -> None:
        """Удалить чанки по id из коллекции; несуществующие игнорируются."""
        ...


class CollectionsStore(ABC):
    """Read-side admin: перечисление и инспекция коллекций."""

    @abstractmethod
    def list_collections(self) -> Iterable[CollectionInfo]:
        """Все коллекции в backend'е."""
        ...

    @abstractmethod
    def collection_info(self, name: CollectionId) -> CollectionInfo:
        """Сводка одной коллекции по имени."""
        ...

    @abstractmethod
    def ensure_collection(
        self,
        name: CollectionId,
        *,
        description: str | None,
    ) -> None:
        """Создать коллекцию name, если отсутствует. Idempotent."""
        ...

    @abstractmethod
    def delete_collection(self, name: CollectionId) -> None:
        """Удалить коллекцию целиком."""
        ...
