"""IndexPipeline: оркестратор Source → Reader → Chunker → Store (полностью lazy)."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator

from boba.indexing.chunks import Chunk
from boba.indexing.context import IndexingContext
from boba.indexing.items import SourceItem
from boba.indexing.sections import Section
from boba.indexing.source import Source
from boba.indexing.stats import IndexStats, IndexStatsBuilder
from boba.indexing.store import Store
from boba.patterns import StateFull, StreamTransformer

__all__ = ["IndexPipeline"]


class IndexPipeline(StateFull):
    """Оркестратор одного прогона индексации.

    Per-item инвариант: для каждого `item.source_id` сначала
    `Store.delete_by_source(...)`, затем upsert новых чанков. Это даёт
    idempotent re-index — повторный запуск с тем же контентом приводит
    к тому же состоянию Store.
    """

    def __init__(
        self,
        *,
        source: Source,
        reader: StreamTransformer[IndexingContext, SourceItem, Section],
        chunker: StreamTransformer[IndexingContext, Section, Chunk],
        store: Store,
    ) -> None:
        self._source = source
        self._reader = reader
        self._chunker = chunker
        self._store = store

    def name(self) -> str:
        return (
            f"IndexPipeline({self._source.name()} → {self._reader.name()}"
            f" → {self._chunker.name()} → {self._store.name()})"
        )

    def reset(self) -> None:
        self._source.reset()
        self._reader.reset()
        self._chunker.reset()
        self._store.reset()

    def run(
        self, ctx: IndexingContext, *, description: str | None = None
    ) -> IndexStats:
        """Полностью lazy: Source.yield → Reader.yield → Chunker.yield → Store.handle.

        Память — O(1) per chunk: ни sections, ни chunks не накапливаются в
        промежуточные list'ы. Только Store держит batch-буфер фиксированного
        размера (упсёрт батчами для производительности).
        """
        self._store.ensure_target(ctx, description)
        stats = IndexStatsBuilder()
        for item in self._source.stream(ctx):
            stats.source_seen(item.source_id)
            stats.chunks_deleted_add(
                self._store.delete_by_source(ctx, item.source_id)
            )

            sections = _tap(
                self._reader.stream(ctx, [item]),
                stats.section_emitted,
            )
            for chunk in self._chunker.stream(ctx, sections):
                self._store.handle(ctx, chunk)
                stats.chunk_upserted()

        self._store.flush(ctx)
        return stats.build()


def _tap(stream: Iterable[Section], on_emit: Callable[[], None]) -> Iterator[Section]:
    """Pass-through итератор + side-effect счётчика. Lazy, без накопления."""
    for section in stream:
        on_emit()
        yield section
