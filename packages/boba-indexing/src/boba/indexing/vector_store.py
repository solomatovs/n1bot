"""VectorStore[T] + CollectionsAdmin — все чистые абстракции работы с векторной базой.

Две ортогональные оси:

1. Document-уровень — VectorStore[T] (чанки внутри коллекции):
   - VectorStoreReader[T]: get_by_ids, similarity_search, peek
   - VectorStoreWriter[T]: upsert, delete
   - VectorStore[T]: композиция

2. Collection-уровень — CollectionsAdmin (CRUD над коллекциями целиком):
   - CollectionsAdminReader: list_collections, collection_info
   - CollectionsAdminWriter: ensure_collection, delete_collection
   - CollectionsAdmin: композиция

Коллекция идентифицируется явным параметром `collection: CollectionId` в
каждом методе VectorStore.

Один store-instance обслуживает много коллекций.

Pipeline-уровень встраивается отдельной обёрткой - `boba.indexing.chunk_sink.ChunkSink`

Embedder[T] инжектится в конкретный backend-impl, не часть контракта.

Конкретный backend обычно реализует и VectorStore[T], и CollectionsAdmin
одной сущностью: например ChromaDBClient — единый класс, реализующий оба
интерфейса.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Generic, TypeVar

from boba.indexing.chunks import Chunk, ChunkId, ChunkSummary
from boba.indexing.context import CollectionId
from boba.indexing.metadata import Metadata
from boba.indexing.sections import SourceId

__all__ = [
    "CollectionInfo",
    "CollectionsAdminReader",
    "CollectionsAdminWriter",
    "SearchHit",
    "VectorStoreReader",
    "VectorStoreWriter",
]

T = TypeVar("T")


@dataclass(frozen=True)
class SearchHit(Generic[T]):
    """
    Один результат поиска над `VectorStore[T]`

    Содержит:
    - `chunk_id`: id найденного чанка, через него можно получить полный
      `Chunk[T]` через `VectorStoreReader.get_by_ids([chunk_id])`

    - `distance`: метрика proximity к query. Семантика **зависит от backend'а**:
      у Chroma — squared-L2 / cosine-distance (меньше = ближе);
      у других провайдеров может быть similarity-score (больше = ближе).
      Backend документирует свою метрику; единого контракта нет.

    - `snippet`: preview выдержка content'а чанка (тип совпадает с T `VectorStore[T]`)
      Для T=str — обрезанный/highlighted текст для UI;
      для T=bytes — thumbnail или сжатый sample.
      Не обязательно равно полному `Chunk.content` — backend решает что класть.

    - `metadata`: метадата чанка (`source_id`, `anchor`, transport/reader/
      chunker-keys и т.п.) для отрисовки в UI без re-fetch'а Chunk'а.
    """

    chunk_id: ChunkId
    distance: float
    snippet: T
    metadata: Metadata = field(default_factory=Metadata.empty)


@dataclass(frozen=True)
class CollectionInfo:
    """Логическая группа векторов в Store (collection в Chroma/Qdrant и т.п.)."""

    name: CollectionId
    description: str
    count: int


class VectorStoreReader(ABC, Generic[T]):
    """Read-side порт: search + inspect документов в коллекции."""

    @abstractmethod
    def get_by_ids(
        self,
        collection: CollectionId,
        chunk_ids: Iterable[ChunkId],
    ) -> Iterable[Chunk[T]]:
        """Получить чанки по id из коллекции; пропускает несуществующие."""
        ...

    @abstractmethod
    def similarity_search(
        self,
        collection: CollectionId,
        *,
        query: T,
        k: int,
    ) -> Iterable[SearchHit[T]]:
        """Семантический поиск top-k в коллекции; query→embedding делает impl."""
        ...

    @abstractmethod
    def peek(
        self,
        collection: CollectionId,
        *,
        source_id: SourceId | None,
        limit: int,
    ) -> Iterable[ChunkSummary[T]]:
        """
        Admin-просмотр: до limit ChunkSummary
        source_id=None — без фильтра
        """
        ...


class VectorStoreWriter(ABC, Generic[T]):
    """Write-side порт: upsert/delete документов в коллекции."""

    @abstractmethod
    def upsert(
        self,
        collection: CollectionId,
        chunks: Iterable[Chunk[T]],
    ) -> None:
        """
        Bulk-upsert чанков в коллекцию
        Atomic per batch не гарантируется
        """
        ...

    @abstractmethod
    def delete(
        self,
        collection: CollectionId,
        chunk_ids: Iterable[ChunkId],
    ) -> None:
        """
        Удалить чанки по id из коллекции
        несуществующие игнорируются
        """
        ...


class CollectionsAdminReader(ABC):
    """Read-side admin: перечисление и инспекция коллекций."""

    @abstractmethod
    def list_collections(self) -> Iterable[CollectionInfo]:
        """Все коллекции в backend'е."""
        ...

    @abstractmethod
    def collection_info(self, name: CollectionId) -> CollectionInfo:
        """Сводка одной коллекции по имени."""
        ...


class CollectionsAdminWriter(ABC):
    """Write-side admin: создание и удаление коллекций."""

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
