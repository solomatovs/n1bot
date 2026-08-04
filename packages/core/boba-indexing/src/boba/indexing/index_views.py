"""Business-layer ABC поверх ChunkStore: IndexQuery[T] (find/clean/narrow со scope-фильтром) и IndexSink[T] (reconcile)."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import ClassVar, Generic, TypeVar

from boba.indexing.chunks import Chunk, ChunkSummary
from boba.indexing.filter import Filter

__all__ = [
    "IndexQuery",
    "IndexSink",
    "ReconcileSummary",
    "TrackingKeys",
]

T = TypeVar("T")


class TrackingKeys:
    """Wire-имена tracking-полей в metadata-store — единый источник правды для всех backend'ов."""

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
    """Filter-based view: реализация инжектит scope-фильтр в каждый запрос, чужой scope недостижим."""

    @abstractmethod
    async def find(
        self,
        *,
        where: Filter | None = None,
        limit: int | None = None,
    ) -> Sequence[ChunkSummary[T]]:
        """Scope-aware поиск по фильтру; where=None — только scope-фильтр, limit=None — без лимита."""
        ...

    @abstractmethod
    async def clean(self, where: Filter) -> int:
        """Удалить чанки scope'а по фильтру, вернуть количество удалённых.

        where обязательный — предохранитель от случайной полной зачистки scope.
        """
        ...

    @abstractmethod
    def narrow(self, where: Filter) -> IndexQuery[T]:
        """Новый IndexQuery с добавленным Filter; каскад narrow(a).narrow(b) ≡ narrow(And([a, b]))."""
        ...


class IndexSink(ABC, Generic[T]):
    """Запись chunk'ов через reconcile с идемпотентной проверкой по content_hash."""

    @abstractmethod
    async def reconcile(
        self,
        chunks: Iterable[Chunk[T]],
        *,
        time_at_least: float,
        force: bool = False,
    ) -> ReconcileSummary:
        """Привести Store в соответствие с чанками (unchanged — только refresh updated_at); force=True — все dirty."""
        ...
