"""
CleanupStrategy — стратегии удаления устаревших записей в конце Indexer.run

три встроенные реализации:
    - NoneCleanup — ничего не удаляет; безопасно для любых прогонов,
      включая частичные
    - IncrementalCleanup — удаляет только записи с touched source_id;
      безопасно для частичных прогонов
    - FullCleanup — удаляет все записи, не обновлённые в этом прогоне;
      требует full-coverage от RequestSource

Пользователь может добавить кастомную (TimeBasedCleanup, SizeBasedCleanup
и т.п.) подклассом `CleanupStrategy`.

Каждая стратегия получает `CleanupContext` — снимок состояния прогона
(run_start, touched_sources) + ссылку на `IndexQuery`. Cleanup делает
только filter-based операции (`clean(where=...)`), поэтому ему хватает
узкого read/delete-контракта без write-возможностей IndexSink.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

from boba.indexing.filter import And, Filter, In, Lt
from boba.indexing.index_views import IndexQuery, TrackingKeys
from boba.indexing.sections import SourceId

__all__ = [
    "CleanupContext",
    "CleanupStrategy",
    "FullCleanup",
    "IncrementalCleanup",
    "NoneCleanup",
]


@dataclass(frozen=True)
class CleanupContext:
    """Снимок состояния одного прогона Indexer.run, передаваемый в стратегию.

    `query` уже привязан к своему scope'у (namespace/tenant/...) —
    стратегии не нужно его конфигурировать; всё что нужно — это
    предикат stale (через Filter DSL) и вызов clean.
    """

    query: IndexQuery[Any]
    run_start: float
    touched_sources: frozenset[SourceId]


class CleanupStrategy(ABC):
    """Стратегия удаления устаревших записей в конце Indexer.run."""

    @abstractmethod
    def execute(self, ctx: CleanupContext) -> int:
        """Выполнить cleanup; вернуть количество удалённых чанков."""
        ...


class NoneCleanup(CleanupStrategy):
    """No-op: ничего не удаляет, всегда возвращает 0.

    Дефолт для безопасных частичных прогонов, когда RequestSource не
    покрывает весь датасет (incremental-фид).
    """

    def execute(self, ctx: CleanupContext) -> int:
        del ctx
        return 0


class IncrementalCleanup(CleanupStrategy):
    """Удалить stale-записи только для touched source_id.

    Безопасно для не-полных прогонов: cleanup затронет только те source_id,
    которые были обработаны в этом прогоне и не получили refresh
    (`updated_at < run_start`).
    """

    def execute(self, ctx: CleanupContext) -> int:
        if not ctx.touched_sources:
            return 0
        where: Filter = And([
            Lt(TrackingKeys.UPDATED_AT, ctx.run_start),
            In(
                TrackingKeys.SOURCE_ID,
                [s.to_wire() for s in ctx.touched_sources],
            ),
        ])
        return ctx.query.clean(where=where)


class FullCleanup(CleanupStrategy):
    """Удалить все stale-записи в текущем scope без фильтра по source_id.

    Требует full-coverage от RequestSource: всё, что не было touched
    в этом прогоне, считается устаревшим. Опасно при частичных фидах —
    удалит актуальные записи.
    """

    def execute(self, ctx: CleanupContext) -> int:
        where: Filter = Lt(TrackingKeys.UPDATED_AT, ctx.run_start)
        return ctx.query.clean(where=where)
