"""CleanupStrategy — стратегии удаления устаревших записей в конце Indexer.run (None/Incremental/Full)."""

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
    """Снимок состояния одного прогона Indexer.run; query уже привязан к scope'у."""

    query: IndexQuery[Any]
    run_start: float
    touched_sources: frozenset[SourceId]


class CleanupStrategy(ABC):
    """Стратегия удаления устаревших записей в конце Indexer.run."""

    @abstractmethod
    async def execute(self, ctx: CleanupContext) -> int:
        """Выполнить cleanup; вернуть количество удалённых чанков."""
        ...


class NoneCleanup(CleanupStrategy):
    """No-op: ничего не удаляет, всегда возвращает 0."""

    async def execute(self, ctx: CleanupContext) -> int:
        del ctx
        return 0


class IncrementalCleanup(CleanupStrategy):
    """Удалить stale-записи только для touched source_id; безопасно при частичных прогонах."""

    async def execute(self, ctx: CleanupContext) -> int:
        if not ctx.touched_sources:
            return 0
        where: Filter = And([
            Lt(TrackingKeys.UPDATED_AT, ctx.run_start),
            In(
                TrackingKeys.SOURCE_ID,
                list(ctx.touched_sources),
            ),
        ])
        return await ctx.query.clean(where=where)


class FullCleanup(CleanupStrategy):
    """Удалить все stale-записи scope'а.

    Требует full-coverage от RequestSource: при частичном фиде удалит актуальные записи.
    """

    async def execute(self, ctx: CleanupContext) -> int:
        where: Filter = Lt(TrackingKeys.UPDATED_AT, ctx.run_start)
        return await ctx.query.clean(where=where)
