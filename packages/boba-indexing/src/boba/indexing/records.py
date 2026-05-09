"""
RecordManager — абстрактный интерфейс для управления записями проиндексированных данных

Отдельная абстракция от VectorStore отвечает за идемпотентность индексации и
базовые операции над записями (exist/list/update/delete),
не вдаваясь в детали chunk'ов и коллекций.

RecordManager — абстракция записей, а не «учётка для VectorStore».
Один RecordManager (например — общая SQL-таблица upsertion_record на сервере)
может одновременно вести учёт для:
- разных коллекций одного агента (namespace="docs", namespace="code", namespace="logs")
- разных агентов на одном backend (namespace="agent-A", namespace="agent-B")
- параллельных pipeline-вариантов (namespace="confluence-prod", namespace="confluence")

RecordManager-таблица:
┌───────────────┬──────────────────┬─────────────┬─────────────┐
│ namespace     │ key (=hash)      │ group_id    │ updated_at  │
├───────────────┼──────────────────┼─────────────┼─────────────┤
│ docs          │ sha256:abc...    │ src/page-1  │ 1700000001  │
│ docs          │ sha256:def...    │ src/page-1  │ 1700000001  │
│ docs          │ sha256:ghi...    │ src/page-2  │ 1700000005  │
│ code          │ sha256:xyz...    │ repo/foo.py │ 1700000010  │
│ logs          │ sha256:rst...    │ run-2024-12 │ 1700000020  │
└───────────────┴──────────────────┴─────────────┴─────────────┘
   ▲              ▲                  ▲
   │              │                  └─ source_id (RecordEntry.group_id)
   │              └─ chunk content_hash (ключ для skip-if-unchanged)
   └─ namespace — изоляция учёта между pipeline'ами / scope'ами
RecordManager может использоваться для трекинга проиндексированных source_id,
для дедупликации, для хранения дополнительных атрибутов и т.п.
в зависимости от конкретной реализации и потребностей pipeline.

Две оси:
1. **Record-уровень** — `RecordManager` (R/W split): exists/list_keys/get_time/
   update/delete_keys.
2. **Backend-admin-уровень** — `RecordsAdmin`: schema-bootstrap, lifecycle
   namespace'ов (create_schema; в будущем list_namespaces, drop_namespace).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable
from dataclasses import dataclass

from boba.indexing.context import NamespaceId

__all__ = [
    "ListKeysQuery",
    "RecordEntry",
    "RecordManagerReader",
    "RecordManagerWriter",
    "RecordsAdmin",
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
        namespace: NamespaceId,
        keys: Iterable[str],
    ) -> Iterable[bool]:
        """True если key зарегистрирован в namespace, иначе False."""
        ...

    @abstractmethod
    def list_keys(
        self,
        namespace: NamespaceId,
        query: ListKeysQuery,
    ) -> Iterable[str]:
        """
        Перечислить keys по фильтрам query
        пустой ListKeysQuery() — означает «все ключи в namespace»
        """
        ...

    @abstractmethod
    def get_time(self) -> float:
        """
        Текущее время backend'а (unix-seconds)
        необходим для time_at_least в update
        """
        ...


class RecordManagerWriter(ABC):
    """регистрация и удаление записей."""

    @abstractmethod
    def update(
        self,
        namespace: NamespaceId,
        entries: Iterable[RecordEntry],
        *,
        time_at_least: float,
    ) -> None:
        """
        Upsert записей в namespace
        updated_at каждой будет >= time_at_least."""
        ...

    @abstractmethod
    def delete_keys(
        self,
        namespace: NamespaceId,
        keys: Iterable[str],
    ) -> None:
        """
        Удалить записи по keys из namespace
        несуществующие игнорируются
        """
        ...


class RecordsAdmin(ABC):
    """
    Admin-операции над backend'ом RecordManager.
    """

    @abstractmethod
    def create_schema(self) -> None:
        """
        Создать таблицу/индексы если отсутствуют
        """
        ...
