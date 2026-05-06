"""PrintPipeline: вывод ChunkSummary через sink + PrintStats."""

from __future__ import annotations

from collections.abc import Iterable

from boba.indexing import Chunk, ChunkSummary, CollectionInfo, Store, StoreId
from boba.processing import IndexingContext
from boba.print_pipeline import PrintPipeline


class _FakeStore(Store):
    """In-memory Store для теста: peek_chunks отдаёт переданные summaries."""

    def __init__(self, summaries: list[ChunkSummary]) -> None:
        self._summaries = summaries
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
        self,
        ctx: IndexingContext,
        *,
        source_id: str | None = None,
        limit: int = 20,
        snippet_chars: int = 200,
    ) -> Iterable[ChunkSummary]:
        self._calls.append({
            "collection": ctx.collection,
            "source_id": source_id,
            "limit": limit,
            "snippet_chars": snippet_chars,
        })
        return [
            s for s in self._summaries
            if source_id is None or s.source_id == source_id
        ][:limit]

    def list_collections(self) -> Iterable[CollectionInfo]:
        return []

    def collection_info(self, name: str) -> CollectionInfo:
        return CollectionInfo(name=name, description="", count=0)

    def delete_collection(self, name: str) -> None:
        del name


def _summary(source_id: str, chunk_index: int, snippet: str) -> ChunkSummary:
    return ChunkSummary(
        chunk_id=f"{source_id}#{chunk_index}",
        source_id=source_id,
        anchor=None,
        chunk_index=chunk_index,
        snippet=snippet,
    )


def test_runs_and_counts_chunks_and_unique_sources():
    summaries = [
        _summary("doc-A", 0, "alpha"),
        _summary("doc-A", 1, "beta"),
        _summary("doc-B", 0, "gamma"),
    ]
    captured: list[str] = []
    stats = PrintPipeline(
        store=_FakeStore(summaries),
        collection="my-coll",
        sink=captured.append,
    ).run()
    assert stats.chunks_printed == 3
    assert stats.sources_seen == 2
    assert len(captured) == 3
    assert "doc-A" in captured[0]
    assert "alpha" in captured[0]


def test_passes_source_id_filter_to_store():
    store = _FakeStore([_summary("doc-A", 0, "x")])
    PrintPipeline(
        store=store, collection="c", source_id="doc-A", sink=lambda _: None,
    ).run()
    assert store.calls[0]["source_id"] == "doc-A"
    assert store.calls[0]["collection"] == "c"


def test_limit_and_snippet_chars_propagated():
    store = _FakeStore([])
    PrintPipeline(
        store=store, collection="c", limit=5, snippet_chars=80,
        sink=lambda _: None,
    ).run()
    assert store.calls[0]["limit"] == 5
    assert store.calls[0]["snippet_chars"] == 80


def test_default_sink_is_print(capsys):
    store = _FakeStore([_summary("doc", 0, "hi")])
    PrintPipeline(store=store, collection="c").run()
    out = capsys.readouterr().out
    assert "doc" in out
    assert "hi" in out


def test_empty_store_yields_zero_stats():
    stats = PrintPipeline(
        store=_FakeStore([]), collection="c", sink=lambda _: None,
    ).run()
    assert stats.chunks_printed == 0
    assert stats.sources_seen == 0


def test_name_includes_collection_and_store():
    pipeline = PrintPipeline(
        store=_FakeStore([]), collection="my-coll", sink=lambda _: None,
    )
    n = pipeline.name()
    assert "my-coll" in n
    assert "FakeStore" in n
