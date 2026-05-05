"""SearchPipeline: Store.search → текстовый sink.

Эмулирует то, что делает `kb_search` агента: оператор задаёт query/top_k,
pipeline превращает текст в embedding (через embedding_function коллекции),
ищет ближайшие чанки, выводит hits с distance + snippet'ом.
"""

from __future__ import annotations

import builtins
from collections.abc import Callable

from boba.indexing import IndexingContext, PipelineId, SearchHit, Store
from boba.patterns import StateFull
from boba.search_pipeline.stats import SearchStats

__all__ = ["SearchPipeline"]


class SearchPipeline(StateFull):
    """Pipeline-оркестратор: семантический поиск + вывод hits."""

    def __init__(  # noqa: PLR0913
        self,
        *,
        store: Store,
        collection: str,
        query: str,
        top_k: int = 5,
        snippet_chars: int = 200,
        sink: Callable[[str], None] = builtins.print,
    ) -> None:
        self._store = store
        self._collection = collection
        self._query = query
        self._top_k = top_k
        self._snippet_chars = snippet_chars
        self._sink = sink

    def name(self) -> str:
        return (
            f"SearchPipeline(collection={self._collection!r}, "
            f"store={self._store.name()})"
        )

    def reset(self) -> None:
        self._store.reset()

    def run(self) -> SearchStats:
        ctx = IndexingContext(
            pipeline_id=PipelineId(f"cli:search:{self._collection}"),
            collection=self._collection,
        )
        embedding_dim = self._store.embedding_dim(ctx)
        hits_returned = 0
        for i, hit in enumerate(self._store.search(
            ctx,
            query=self._query,
            top_k=self._top_k,
            snippet_chars=self._snippet_chars,
        )):
            hits_returned += 1
            self._sink(_format_hit(i, hit))
        return SearchStats(
            hits_returned=hits_returned,
            embedding_dim=embedding_dim,
        )


def _format_hit(rank: int, h: SearchHit) -> str:
    """Многострочный формат: rank, distance, source, snippet."""
    source = h.metadata.get("source_id") or h.metadata.get("source_url") or ""
    anchor = h.metadata.get("anchor") or ""
    link = f"{source}#{anchor}" if source and anchor else source
    return (
        f"#{rank + 1}  distance={h.distance:.4f}  [{h.id}]\n"
        f"  {link}\n"
        f"  {h.snippet}"
    )
