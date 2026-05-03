"""IndexPipeline end-to-end на in-memory Source/Reader/Chunker/Store."""

from __future__ import annotations

from collections.abc import Iterable

import pytest

from boba.indexing import (
    Chunk,
    Chunker,
    ChunkerId,
    CollectionInfo,
    IndexingContext,
    IndexPipeline,
    NoMatchingReaderError,
    PipelineId,
    Reader,
    ReaderDispatcher,
    ReaderId,
    Section,
    Source,
    SourceId,
    SourceItem,
    Store,
    StoreId,
)


class _MemSource(Source):
    """Source с зашитым набором SourceItem."""

    def __init__(self, items: list[SourceItem]) -> None:
        self._items = items

    def name(self) -> str:
        return "MemSource"

    def source_factory_id(self) -> SourceId:
        return SourceId("mem")

    def stream(self, ctx: IndexingContext) -> Iterable[SourceItem]:
        del ctx
        yield from self._items


class _OneToOneReader(Reader):
    """Reader, который принимает один hint и выдаёт ровно одну Section."""

    def __init__(self, hint: str) -> None:
        self._hint = hint

    def name(self) -> str:
        return f"OneToOneReader({self._hint})"

    def reader_id(self) -> ReaderId:
        return ReaderId(self._hint)

    def accepts(self, item: SourceItem) -> bool:
        return item.content_hint == self._hint

    def convert(
        self, ctx: IndexingContext, value: SourceItem
    ) -> Iterable[Section]:
        del ctx
        yield Section(
            source_id=value.source_id,
            text=value.payload.decode("utf-8"),
            anchor=None,
            order=0,
        )


class _IdentityChunker(Chunker):
    """Chunker, который превращает каждую Section в один Chunk 1-к-1."""

    def name(self) -> str:
        return "IdentityChunker"

    def chunker_id(self) -> ChunkerId:
        return ChunkerId("identity")

    def stream(
        self, ctx: IndexingContext, stream: Iterable[Section]
    ) -> Iterable[Chunk]:
        del ctx
        for s in stream:
            yield Chunk(
                chunk_id=f"{s.source_id}#0",
                source_id=s.source_id,
                text=s.text,
                anchor=s.anchor,
                chunk_index=0,
            )


class _MemStore(Store):
    """Store с апсертом в dict[chunk_id, Chunk] и delete_by_source."""

    def __init__(self) -> None:
        self.chunks: dict[str, Chunk] = {}

    def name(self) -> str:
        return "MemStore"

    def store_id(self) -> StoreId:
        return StoreId("mem")

    def handle(self, ctx: IndexingContext, event: Chunk) -> None:
        del ctx
        self.chunks[event.chunk_id] = event

    def delete_by_source(self, ctx: IndexingContext, source_id: str) -> int:
        del ctx
        ids = [k for k, v in self.chunks.items() if v.source_id == source_id]
        for k in ids:
            del self.chunks[k]
        return len(ids)

    def list_source_ids(self, ctx: IndexingContext) -> Iterable[str]:
        del ctx
        return {c.source_id for c in self.chunks.values()}

    def list_collections(self) -> Iterable[CollectionInfo]:
        return [CollectionInfo(name="mem", description="", count=len(self.chunks))]

    def collection_info(self, name: str) -> CollectionInfo:
        return CollectionInfo(name=name, description="", count=len(self.chunks))

    def delete_collection(self, name: str) -> None:
        del name
        self.chunks.clear()


def _ctx() -> IndexingContext:
    return IndexingContext(pipeline_id=PipelineId("test"), collection="test")


def _item(source_id: str, hint: str, text: str) -> SourceItem:
    return SourceItem(
        source_id=source_id,
        content_hint=hint,
        payload=text.encode("utf-8"),
    )


def test_pipeline_indexes_each_item_once():
    src = _MemSource([_item("mem:/a", "txt", "hello"), _item("mem:/b", "txt", "world")])
    store = _MemStore()
    pipeline = IndexPipeline(
        source=src,
        reader=ReaderDispatcher([_OneToOneReader("txt")]),
        chunker=_IdentityChunker(),
        store=store,
    )

    stats = pipeline.run(_ctx())

    assert stats.sources_processed == 2
    assert stats.chunks_upserted == 2
    assert stats.chunks_deleted == 0
    assert set(store.chunks) == {"mem:/a#0", "mem:/b#0"}


def test_pipeline_reindex_deletes_previous_chunks():
    src1 = _MemSource([_item("mem:/a", "txt", "v1")])
    src2 = _MemSource([_item("mem:/a", "txt", "v2")])
    store = _MemStore()

    p1 = IndexPipeline(
        source=src1,
        reader=ReaderDispatcher([_OneToOneReader("txt")]),
        chunker=_IdentityChunker(),
        store=store,
    )
    p1.run(_ctx())
    assert store.chunks["mem:/a#0"].text == "v1"

    p2 = IndexPipeline(
        source=src2,
        reader=ReaderDispatcher([_OneToOneReader("txt")]),
        chunker=_IdentityChunker(),
        store=store,
    )
    stats = p2.run(_ctx())

    assert stats.chunks_deleted == 1
    assert stats.chunks_upserted == 1
    assert store.chunks["mem:/a#0"].text == "v2"


def test_dispatcher_picks_first_matching_reader():
    src = _MemSource([
        _item("mem:/a", "md", "md-text"),
        _item("mem:/b", "html", "html-text"),
    ])
    store = _MemStore()
    pipeline = IndexPipeline(
        source=src,
        reader=ReaderDispatcher([_OneToOneReader("md"), _OneToOneReader("html")]),
        chunker=_IdentityChunker(),
        store=store,
    )

    stats = pipeline.run(_ctx())

    assert stats.chunks_upserted == 2
    assert store.chunks["mem:/a#0"].text == "md-text"
    assert store.chunks["mem:/b#0"].text == "html-text"


def test_dispatcher_raises_when_no_reader_matches():
    src = _MemSource([_item("mem:/a", "pdf", "pdf-bytes")])
    pipeline = IndexPipeline(
        source=src,
        reader=ReaderDispatcher([_OneToOneReader("md")]),
        chunker=_IdentityChunker(),
        store=_MemStore(),
    )

    with pytest.raises(NoMatchingReaderError) as excinfo:
        pipeline.run(_ctx())

    assert excinfo.value.source_id == "mem:/a"
    assert excinfo.value.content_hint == "pdf"


def test_dispatcher_skip_unmatched_when_enabled():
    src = _MemSource([
        _item("mem:/a", "md", "md-text"),
        _item("mem:/b", "pdf", "pdf-bytes"),
    ])
    store = _MemStore()
    pipeline = IndexPipeline(
        source=src,
        reader=ReaderDispatcher(
            [_OneToOneReader("md")], skip_unmatched=True
        ),
        chunker=_IdentityChunker(),
        store=store,
    )

    stats = pipeline.run(_ctx())

    assert stats.chunks_upserted == 1
    assert "mem:/a#0" in store.chunks


def test_pipeline_reset_propagates_to_stages():
    class _Counting(Chunker):
        reset_calls = 0

        def name(self) -> str:
            return "Counting"

        def chunker_id(self) -> ChunkerId:
            return ChunkerId("counting")

        def stream(
            self, ctx: IndexingContext, stream: Iterable[Section]
        ) -> Iterable[Chunk]:
            del ctx
            yield from ()

        def reset(self) -> None:
            type(self).reset_calls += 1

    chunker = _Counting()
    pipeline = IndexPipeline(
        source=_MemSource([]),
        reader=ReaderDispatcher([_OneToOneReader("md")]),
        chunker=chunker,
        store=_MemStore(),
    )
    pipeline.reset()

    assert _Counting.reset_calls == 1
