"""CollectionsAdmin: админ-операции над коллекциями (CRUD на уровне namespace).

Отдельная ось от VectorStore — там document-уровень (upsert/delete/search чанков),
здесь collection-уровень (создать/удалить/перечислить namespace'ы).

Read/Write split (как VectorStoreReader/Writer):
- CollectionsAdminReader: list_collections, collection_info
- CollectionsAdminWriter: ensure_collection, delete_collection
- CollectionsAdmin: композиция
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable

from boba.indexing.collections import CollectionInfo

__all__ = [
    "CollectionsAdmin",
    "CollectionsAdminReader",
    "CollectionsAdminWriter",
]


class CollectionsAdminReader(ABC):
    """Read-side admin: перечисление и инспекция коллекций."""

    @abstractmethod
    def list_collections(self) -> Iterable[CollectionInfo]:
        """Все коллекции в backend'е."""
        ...

    @abstractmethod
    def collection_info(self, name: str) -> CollectionInfo:
        """Сводка одной коллекции по имени."""
        ...


class CollectionsAdminWriter(ABC):
    """Write-side admin: создание и удаление коллекций."""

    @abstractmethod
    def ensure_collection(
        self,
        name: str,
        *,
        description: str | None,
    ) -> None:
        """Создать коллекцию name, если отсутствует. Idempotent."""
        ...

    @abstractmethod
    def delete_collection(self, name: str) -> None:
        """Удалить коллекцию целиком."""
        ...


class CollectionsAdmin(CollectionsAdminReader, CollectionsAdminWriter, ABC):
    """Композиция Reader + Writer для impls, делающих и то и другое."""
