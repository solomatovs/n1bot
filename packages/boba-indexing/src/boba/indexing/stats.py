"""Накапливаемая статистика одного запуска IndexPipeline."""

from __future__ import annotations

from dataclasses import dataclass, field

__all__ = ["IndexStats", "IndexStatsBuilder"]


@dataclass(frozen=True)
class IndexStats:
    """Сводка одного IndexPipeline.run()."""

    sources_processed: int
    sections_emitted: int
    chunks_upserted: int
    chunks_deleted: int


@dataclass
class IndexStatsBuilder:
    """Мутабельный аккумулятор для сборки IndexStats внутри pipeline."""

    sources_processed: int = 0
    sections_emitted: int = 0
    chunks_upserted: int = 0
    chunks_deleted: int = 0
    _seen_sources: set[str] = field(default_factory=set)

    def source_seen(self, source_id: str) -> None:
        if source_id not in self._seen_sources:
            self._seen_sources.add(source_id)
            self.sources_processed += 1

    def section_emitted(self) -> None:
        self.sections_emitted += 1

    def chunk_upserted(self) -> None:
        self.chunks_upserted += 1

    def chunks_deleted_add(self, n: int) -> None:
        self.chunks_deleted += n

    def build(self) -> IndexStats:
        return IndexStats(
            sources_processed=self.sources_processed,
            sections_emitted=self.sections_emitted,
            chunks_upserted=self.chunks_upserted,
            chunks_deleted=self.chunks_deleted,
        )
