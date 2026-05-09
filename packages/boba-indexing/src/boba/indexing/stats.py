"""
Накапливаемая статистика одного запуска IndexPipeline
для логирования, метрик, CLI-отчётов, и т.п.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from boba.indexing.sections import SourceId

__all__ = ["IndexStats", "IndexStatsBuilder"]


@dataclass(frozen=True)
class IndexStats:
    """Сводка одного IndexPipeline.run()."""

    sources_processed: int
    sources_failed: int
    sources_skipped_unchanged: int
    sections_emitted: int
    chunks_upserted: int
    chunks_deleted: int


@dataclass
class IndexStatsBuilder:
    """Мутабельный аккумулятор для сборки IndexStats внутри pipeline."""

    sources_processed: int = 0
    sources_failed: int = 0
    sources_skipped_unchanged: int = 0
    sections_emitted: int = 0
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

    def section_emitted(self) -> None:
        self.sections_emitted += 1

    def chunk_upserted(self) -> None:
        self.chunks_upserted += 1

    def chunks_deleted_add(self, n: int) -> None:
        self.chunks_deleted += n

    def build(self) -> IndexStats:
        return IndexStats(
            sources_processed=self.sources_processed,
            sources_failed=self.sources_failed,
            sources_skipped_unchanged=self.sources_skipped_unchanged,
            sections_emitted=self.sections_emitted,
            chunks_upserted=self.chunks_upserted,
            chunks_deleted=self.chunks_deleted,
        )
