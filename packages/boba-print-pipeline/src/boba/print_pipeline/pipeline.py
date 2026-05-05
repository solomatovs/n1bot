"""PrintPipeline: Store.peek_chunks → текстовый sink.

Read-only оркестратор для оператора: фиксированная коллекция, опциональный
фильтр по `source_id`, лимит и длина snippet'а. Сам Pipeline ничего не
знает про источники Markdown/HTML/Confluence — он работает с уже
проиндексированными `Chunk`-ами в Store.

`sink: Callable[[str], None]` инжектится снаружи: по умолчанию `print`,
но caller может подменить на logger, BytesIO-буфер для теста, etc.
"""

from __future__ import annotations

import builtins
from collections.abc import Callable

from boba.indexing import IndexingContext, PipelineId, Store
from boba.patterns import StateFull
from boba.print_pipeline.stats import PrintStats

__all__ = ["PrintPipeline"]


class PrintPipeline(StateFull):
    """Pipeline-оркестратор: вывод чанков коллекции в текстовый sink."""

    def __init__(  # noqa: PLR0913
        self,
        *,
        store: Store,
        collection: str,
        source_id: str | None = None,
        limit: int = 20,
        snippet_chars: int = 200,
        sink: Callable[[str], None] = builtins.print,
    ) -> None:
        self._store = store
        self._collection = collection
        self._source_id = source_id
        self._limit = limit
        self._snippet_chars = snippet_chars
        self._sink = sink

    def name(self) -> str:
        return (
            f"PrintPipeline(collection={self._collection!r}, "
            f"store={self._store.name()})"
        )

    def reset(self) -> None:
        self._store.reset()

    def run(self) -> PrintStats:
        ctx = IndexingContext(
            pipeline_id=PipelineId(f"cli:print:{self._collection}"),
            collection=self._collection,
        )
        embedding_dim = self._store.embedding_dim(ctx)
        seen_sources: set[str] = set()
        chunks_printed = 0
        for summary in self._store.peek_chunks(
            ctx,
            source_id=self._source_id,
            limit=self._limit,
            snippet_chars=self._snippet_chars,
        ):
            seen_sources.add(summary.source_id)
            chunks_printed += 1
            self._sink(_format_chunk(summary))
        return PrintStats(
            chunks_printed=chunks_printed,
            sources_seen=len(seen_sources),
            embedding_dim=embedding_dim,
        )


def _format_chunk(s: object) -> str:
    """Однострочный формат: source_id#anchor:idx — snippet."""
    chunk_id = getattr(s, "chunk_id", "")
    source_id = getattr(s, "source_id", "")
    anchor = getattr(s, "anchor", None) or "-"
    idx = getattr(s, "chunk_index", 0)
    snippet = getattr(s, "snippet", "")
    head = f"{source_id}#{anchor}:{idx}"
    return f"{head}  [{chunk_id}]\n  {snippet}"
