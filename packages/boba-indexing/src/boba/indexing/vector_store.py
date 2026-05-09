"""VectorStore: document-уровень — upsert/delete/get_by_ids/similarity_search.

Read/Write split по конвенции проекта (как MessageReader/MessageWriter):
- VectorStoreReader: get_by_ids, similarity_search, peek
- VectorStoreWriter: upsert, delete
- VectorStore: композиция

Collection-уровень (list/ensure/delete коллекции) — отдельная ось, см.
boba.indexing.collections_admin.CollectionsAdmin.

Embedder инжектится в конкретный impl, не часть контракта VectorStore.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

from boba.indexing.chunks import Chunk, ChunkSummary
from boba.processing import IndexingContext

__all__ = [
    "SearchHit",
    "VectorStore",
    "VectorStoreReader",
    "VectorStoreWriter",
]


@dataclass(frozen=True)
class SearchHit:
    """Результат similarity-search; distance — нативный (меньше = ближе)."""

    chunk_id: str
    distance: float
    snippet: str
    metadata: Mapping[str, str] = field(default_factory=dict)


class VectorStoreReader(ABC):
    """Read-side порт: search + inspect документов в коллекции."""

    @abstractmethod
    def get_by_ids(
        self,
        ctx: IndexingContext,
        chunk_ids: Iterable[str],
    ) -> Iterable[Chunk]:
        """Получить чанки по id; пропускает несуществующие."""
        ...

    @abstractmethod
    def similarity_search(
        self,
        ctx: IndexingContext,
        *,
        query: str,
        k: int,
    ) -> Iterable[SearchHit]:
        """Семантический поиск top-k; query→embedding делает impl."""
        ...

    @abstractmethod
    def peek(
        self,
        ctx: IndexingContext,
        *,
        source_id: str | None,
        limit: int,
    ) -> Iterable[ChunkSummary]:
        """Admin-просмотр: до limit ChunkSummary; source_id=None — без фильтра."""
        ...


class VectorStoreWriter(ABC):
    """Write-side порт: upsert/delete документов в коллекции."""

    @abstractmethod
    def upsert(
        self,
        ctx: IndexingContext,
        chunks: Iterable[Chunk],
    ) -> None:
        """Bulk-upsert чанков в `ctx.collection`. Atomic per batch не гарантируется."""
        ...

    @abstractmethod
    def delete(
        self,
        ctx: IndexingContext,
        chunk_ids: Iterable[str],
    ) -> None:
        """Удалить чанки по id; несуществующие игнорируются."""
        ...


class VectorStore(VectorStoreReader, VectorStoreWriter, ABC):
    """Композиция Reader + Writer для impls, делающих и то и другое."""
