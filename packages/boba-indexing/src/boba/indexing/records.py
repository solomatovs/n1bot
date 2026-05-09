from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable
from dataclasses import dataclass

from boba.processing import IndexingContext

__all__ = [
    "ListKeysQuery",
    "RecordEntry",
    "RecordManager",
    "RecordManagerReader",
    "RecordManagerWriter",
]


@dataclass(frozen=True)
class ListKeysQuery:
    """Фильтры для RecordManagerReader.list_keys"""

    group_ids: Iterable[str] | None = None
    before: float | None = None
    after: float | None = None
    limit: int | None = None


@dataclass(frozen=True)
class RecordEntry:
    """Запись для RecordManagerWriter.update: ключ + опциональный group_id."""

    key: str
    group_id: str | None = None


class RecordManagerReader(ABC):
    """Читает проиндексированные записи"""

    @abstractmethod
    def exists(
        self,
        ctx: IndexingContext,
        keys: Iterable[str],
    ) -> Iterable[bool]:
        """True если key зарегистрирован, иначе False."""
        ...

    @abstractmethod
    def list_keys(
        self,
        ctx: IndexingContext,
        query: ListKeysQuery,
    ) -> Iterable[str]:
        """Перечислить keys по фильтрам query; пустой ListKeysQuery() — все."""
        ...

    @abstractmethod
    def get_time(self) -> float:
        """
        Текущее время backend'а (unix-seconds); для time_at_least в update
        """
        ...


class RecordManagerWriter(ABC):
    """регистрация и удаление записей."""

    @abstractmethod
    def update(
        self,
        ctx: IndexingContext,
        entries: Iterable[RecordEntry],
        *,
        time_at_least: float,
    ) -> None:
        """Upsert записей; updated_at каждой будет >= time_at_least."""
        ...

    @abstractmethod
    def delete_keys(
        self,
        ctx: IndexingContext,
        keys: Iterable[str],
    ) -> None:
        """Удалить записи по keys; несуществующие игнорируются."""
        ...

    @abstractmethod
    def create_schema(self) -> None:
        """Создать таблицу/индексы если отсутствуют. Idempotent. Bootstrap-only."""
        ...


class RecordManager(RecordManagerReader, RecordManagerWriter, ABC):
    """Композиция Reader + Writer для impls, делающих и то и другое."""
