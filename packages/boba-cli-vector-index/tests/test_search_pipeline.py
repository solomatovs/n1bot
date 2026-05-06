"""SearchPipeline: вывод SearchHit'ов через sink + SearchStats."""

from __future__ import annotations

from collections.abc import Iterable

from boba.indexing import (
    Chunk,
    ChunkSummary,
    CollectionInfo,
    SearchHit,
    Store,
    StoreId,
)
from boba.processing import IndexingContext
from boba.cli.vector_index.search_pipeline import SearchPipeline


class _FakeStore(Store):
    """Store, отдающий заданные SearchHit'ы и фиксирующий вызовы."""

    def __init__(
        self,
        hits: list[SearchHit],
        embedding_dim: int = 384,
    ) -> None:
        self._hits = hits
        self._dim = embedding_dim
        self._calls: list[dict[str, object]] = []

    @property
    def calls(self) -> list[dict[str, object]]:
        return self._calls

    def name(self) -> str:
        return "FakeStore"

    def store_id(self) -> StoreId:
        return StoreId("fake")

    def handle(self, ctx: IndexingContext, item: Chunk) -> None:
        del ctx, item

    def delete_by_source(self, ctx: IndexingContext, source_id: str) -> int:
        del ctx, source_id
        return 0

    def list_source_ids(self, ctx: IndexingContext) -> Iterable[str]:
        del ctx
        return []

    def peek_chunks(
        self, ctx: IndexingContext, *, source_id: str | None = None,
        limit: int = 20, snippet_chars: int = 200,
    ) -> Iterable[ChunkSummary]:
        del ctx, source_id, limit, snippet_chars
        return []

    def search(
        self, ctx: IndexingContext, *,
        query: str, top_k: int = 5, snippet_chars: int = 200,
    ) -> Iterable[SearchHit]:
        self._calls.append({
            "collection": ctx.collection,
            "query": query,
            "top_k": top_k,
            "snippet_chars": snippet_chars,
        })
        return self._hits[:top_k]

    def embedding_dim(self, ctx: IndexingContext) -> int:
        del ctx
        return self._dim

    def list_collections(self) -> Iterable[CollectionInfo]:
        return []

    def collection_info(self, name: str) -> CollectionInfo:
        return CollectionInfo(name=name, description="", count=0)

    def delete_collection(self, name: str) -> None:
        del name


def _hit(idx: int, dist: float, snippet: str) -> SearchHit:
    return SearchHit(
        id=f"chunk-{idx}",
        distance=dist,
        snippet=snippet,
        metadata={"source_id": f"doc-{idx}"},
    )


def test_runs_and_returns_stats():
    hits = [_hit(0, 0.1, "alpha"), _hit(1, 0.3, "beta"), _hit(2, 0.5, "gamma")]
    captured: list[str] = []
    stats = SearchPipeline(
        store=_FakeStore(hits, embedding_dim=1024),
        collection="docs", query="hello", top_k=3,
        sink=captured.append,
    ).run()
    assert stats.hits_returned == 3
    assert stats.embedding_dim == 1024
    assert len(captured) == 3
    assert "alpha" in captured[0]
    assert "0.1000" in captured[0]


def test_top_k_propagated():
    store = _FakeStore([_hit(0, 0.1, "x")])
    SearchPipeline(
        store=store, collection="c", query="q", top_k=7, sink=lambda _: None,
    ).run()
    assert store.calls[0]["top_k"] == 7
    assert store.calls[0]["query"] == "q"
    assert store.calls[0]["collection"] == "c"


def test_empty_hits():
    stats = SearchPipeline(
        store=_FakeStore([]), collection="c", query="q", sink=lambda _: None,
    ).run()
    assert stats.hits_returned == 0


def test_default_sink_is_print(capsys):
    SearchPipeline(
        store=_FakeStore([_hit(0, 0.2, "snippet")]),
        collection="c", query="q",
    ).run()
    out = capsys.readouterr().out
    assert "snippet" in out
    assert "0.2000" in out


def test_name_includes_collection_and_store():
    n = SearchPipeline(
        store=_FakeStore([]), collection="my-coll", query="q", sink=lambda _: None,
    ).name()
    assert "my-coll" in n
    assert "FakeStore" in n
