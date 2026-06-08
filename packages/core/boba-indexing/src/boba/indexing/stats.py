"""
Накапливаемая статистика одного запуска Pipeline.index
для логирования, метрик, CLI-отчётов, и т.п.

Поля 1:1 соответствуют CompletedItem-events: каждый бамп счётчика
происходит ровно при эмите соответствующего event'а через
Pipeline._observe. Builder здесь — это «view над event stream'ом
в виде счётчиков», а не отдельный накопитель данных.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from boba.indexing.sections import SourceId

__all__ = ["IndexStats", "IndexStatsBuilder"]


@dataclass(frozen=True)
class IndexStats:
    """Сводка одного Pipeline.run() / .index()."""

    sources_processed: int
    sources_failed: int
    sources_skipped_unchanged: int
    chunks_upserted: int
    chunks_deleted: int


@dataclass
class IndexStatsBuilder:
    """Мутабельный аккумулятор IndexStats, обновляемый из event stream'а."""

    sources_processed: int = 0
    sources_failed: int = 0
    sources_skipped_unchanged: int = 0
    chunks_upserted: int = 0
    chunks_deleted: int = 0
    _seen_sources: set[SourceId] = field(default_factory=set)

    def source_seen(self, source_id: SourceId) -> None:
        if source_id not in self._seen_sources:
            self._seen_sources.add(source_id)
            self.sources_processed += 1

    def source_failed(self) -> None:
        self.sources_failed += 1

    def source_skipped_unchanged(self) -> None:
        self.sources_skipped_unchanged += 1

    def chunk_upserted(self) -> None:
        self.chunks_upserted += 1

    def chunks_deleted_add(self, n: int) -> None:
        self.chunks_deleted += n

    def build(self) -> IndexStats:
        return IndexStats(
            sources_processed=self.sources_processed,
            sources_failed=self.sources_failed,
            sources_skipped_unchanged=self.sources_skipped_unchanged,
            chunks_upserted=self.chunks_upserted,
            chunks_deleted=self.chunks_deleted,
        )
