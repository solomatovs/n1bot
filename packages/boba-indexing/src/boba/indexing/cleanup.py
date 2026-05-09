"""
CleanupStrategy - стратегии удаления устаревших записей в конце Indexer.run

три встроенные реализации:
    - NoneCleanup - ничего не удаляет, безопасно для любых прогонов, включая частичные
    - IncrementalCleanup - удаляет только записи с touched source_id
        безопасно для частичных прогонов
    - FullCleanup - удаляет все записи, не обновлённые в этом прогоне
        требует full-coverage от RequestSource

Пользователь может добавить кастомную
например TimeBasedCleanup, удаляющий записи старше N дней) подклассом `CleanupStrategy`

Каждая стратегия получает `CleanupContext` — снимок состояния прогона
(namespace, collection, run_start, touched_sources) + ссылки на хранилища
(record_manager, vector_store) — и возвращает число удалённых чанков
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

from boba.indexing.chunks import ChunkId
from boba.indexing.context import CollectionId, NamespaceId
from boba.indexing.records import (
    ListKeysQuery,
    RecordManagerReader,
    RecordManagerWriter,
)
from boba.indexing.sections import SourceId
from boba.indexing.vector_store import VectorStoreWriter

__all__ = [
    "CleanupContext",
    "CleanupStrategy",
    "FullCleanup",
    "IncrementalCleanup",
    "NoneCleanup",
]


@dataclass(frozen=True)
class CleanupContext:
    """
    Снимок состояния одного прогона Indexer.run, передаваемый в стратегию

    `vector_store` приходит c Any типом, потому что больше им и не надо
    `delete(collection, chunk_ids)`, конкретный T-параметр не нужен
    """

    namespace: NamespaceId
    collection: CollectionId
    record_manager_writer: RecordManagerWriter
    record_manager_reader: RecordManagerReader
    vector_store: VectorStoreWriter[Any]
    run_start: float
    touched_sources: frozenset[SourceId]


class CleanupStrategy(ABC):
    """Стратегия удаления устаревших записей в конце Indexer.run."""

    @abstractmethod
    def execute(self, ctx: CleanupContext) -> int:
        """Выполнить cleanup; вернуть количество удалённых чанков."""
        ...


class NoneCleanup(CleanupStrategy):
    """
    No-op: ничего не удаляет, всегда возвращает 0.

    Дефолт для безопасных частичных прогонов, когда RequestSource не
    покрывает весь датасет (incremental-фид)
    """

    def execute(self, ctx: CleanupContext) -> int:
        del ctx
        return 0


class IncrementalCleanup(CleanupStrategy):
    """
    Удалить stale записи только для touched-source_id

    Безопасно для не полных-прогонов: cleanup затронет только те source_id,
    которые были обработаны в этом прогоне и не получили refresh
    (updated_at < run_start)
    """

    def execute(self, ctx: CleanupContext) -> int:
        stale_keys = list(
            ctx.record_manager_reader.list_keys(
                ctx.namespace,
                ListKeysQuery(
                    group_ids=[s.to_wire() for s in ctx.touched_sources],
                    before=ctx.run_start,
                ),
            )
        )
        if not stale_keys:
            return 0

        ctx.vector_store.delete(
            ctx.collection,
            (ChunkId(k) for k in stale_keys),
        )

        ctx.record_manager_writer.delete_keys(ctx.namespace, stale_keys)

        return len(stale_keys)


class FullCleanup(CleanupStrategy):
    """
    Удалить все stale записи в namespace без фильтра по source_id

    Требует full-coverage от RequestSource
    всё, что не было touched в этом прогоне, считается устаревшим.
    Опасно при частичных фидах — удалит
    актуальные записи.
    """

    def execute(self, ctx: CleanupContext) -> int:
        stale_keys = list(
            ctx.record_manager_reader.list_keys(
                ctx.namespace,
                ListKeysQuery(before=ctx.run_start),
            )
        )
        if not stale_keys:
            return 0

        ctx.vector_store.delete(
            ctx.collection,
            (ChunkId(k) for k in stale_keys),
        )

        ctx.record_manager_writer.delete_keys(ctx.namespace, stale_keys)

        return len(stale_keys)
