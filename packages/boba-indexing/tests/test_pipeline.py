"""IndexPipeline end-to-end на in-memory Source/Reader/Chunker/Store."""

from __future__ import annotations

from collections.abc import Iterable

from boba.indexing import (
    Chunk,
    Chunker,
    ChunkerId,
    CollectionInfo,
    IndexingContext,
    IndexPipeline,
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

    def list_source_ids(self) -> Iterable[str]:
        return [i.source_id for i in self._items]


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
            content_hash=value.content_hash,
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
                content_hash=s.content_hash,
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

    def content_hash_for(self, ctx: IndexingContext, source_id: str) -> str:
        del ctx
        for c in self.chunks.values():
            if c.source_id == source_id and c.content_hash:
                return c.content_hash
        return ""

    def list_collections(self) -> Iterable[CollectionInfo]:
        return [CollectionInfo(name="mem", description="", count=len(self.chunks))]

    def collection_info(self, name: str) -> CollectionInfo:
        return CollectionInfo(name=name, description="", count=len(self.chunks))

    def delete_collection(self, name: str) -> None:
        del name
        self.chunks.clear()


def _ctx() -> IndexingContext:
    return IndexingContext(pipeline_id=PipelineId("test"), collection="test")


def _item(
    source_id: str, hint: str, text: str, *, content_hash: str = ""
) -> SourceItem:
    return SourceItem(
        source_id=source_id,
        content_hint=hint,
        payload=text.encode("utf-8"),
        content_hash=content_hash,
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


def test_dispatcher_failure_isolated_per_item():
    """Per-item error handling: упавший item учитывается в sources_failed,
    остальные продолжают обрабатываться. Раньше pipeline ронял весь прогон —
    теперь ошибка одной страницы не должна блокировать сотню других."""
    src = _MemSource([
        _item("mem:/ok", "md", "md-text"),
        _item("mem:/bad", "pdf", "pdf-bytes"),
        _item("mem:/ok2", "md", "md-text-2"),
    ])
    store = _MemStore()
    pipeline = IndexPipeline(
        source=src,
        reader=ReaderDispatcher([_OneToOneReader("md")]),
        chunker=_IdentityChunker(),
        store=store,
    )

    stats = pipeline.run(_ctx())

    assert stats.sources_failed == 1
    assert stats.sources_processed == 2
    assert stats.chunks_upserted == 2
    assert "mem:/ok#0" in store.chunks
    assert "mem:/ok2#0" in store.chunks
    # упавший item не должен оставить чанков
    assert not any(c.startswith("mem:/bad") for c in store.chunks)


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


def test_incremental_skips_unchanged_items():
    """Если content_hash не изменился — Pipeline пропускает item полностью."""
    src1 = _MemSource([_item("mem:/a", "md", "v1-text", content_hash="v1")])
    store = _MemStore()
    pipeline1 = IndexPipeline(
        source=src1,
        reader=ReaderDispatcher([_OneToOneReader("md")]),
        chunker=_IdentityChunker(),
        store=store,
    )
    stats1 = pipeline1.run(_ctx())
    assert stats1.sources_processed == 1
    assert stats1.sources_skipped_unchanged == 0
    assert store.chunks["mem:/a#0"].content_hash == "v1"

    # Повторный прогон с тем же content_hash — skip.
    src2 = _MemSource([_item("mem:/a", "md", "v1-text-IGNORED", content_hash="v1")])
    pipeline2 = IndexPipeline(
        source=src2,
        reader=ReaderDispatcher([_OneToOneReader("md")]),
        chunker=_IdentityChunker(),
        store=store,
    )
    stats2 = pipeline2.run(_ctx())
    assert stats2.sources_skipped_unchanged == 1
    assert stats2.sources_processed == 0
    # Текст не изменился — IGNORED содержимое не попало в Store.
    assert store.chunks["mem:/a#0"].text == "v1-text"


def test_incremental_reindexes_when_hash_changes():
    """Изменение content_hash → Pipeline переиндексирует item."""
    src1 = _MemSource([_item("mem:/a", "md", "v1", content_hash="v1")])
    store = _MemStore()
    pipeline1 = IndexPipeline(
        source=src1,
        reader=ReaderDispatcher([_OneToOneReader("md")]),
        chunker=_IdentityChunker(),
        store=store,
    )
    pipeline1.run(_ctx())

    src2 = _MemSource([_item("mem:/a", "md", "v2", content_hash="v2")])
    pipeline2 = IndexPipeline(
        source=src2,
        reader=ReaderDispatcher([_OneToOneReader("md")]),
        chunker=_IdentityChunker(),
        store=store,
    )
    stats = pipeline2.run(_ctx())

    assert stats.sources_processed == 1
    assert stats.sources_skipped_unchanged == 0
    assert store.chunks["mem:/a#0"].text == "v2"
    assert store.chunks["mem:/a#0"].content_hash == "v2"


def test_no_content_hash_always_reindexes():
    """Если SourceItem.content_hash пуст — incremental не работает (всегда upsert)."""
    src = _MemSource([_item("mem:/a", "md", "first")])
    store = _MemStore()
    pipeline = IndexPipeline(
        source=src,
        reader=ReaderDispatcher([_OneToOneReader("md")]),
        chunker=_IdentityChunker(),
        store=store,
    )
    pipeline.run(_ctx())
    src2 = _MemSource([_item("mem:/a", "md", "second")])
    pipeline2 = IndexPipeline(
        source=src2,
        reader=ReaderDispatcher([_OneToOneReader("md")]),
        chunker=_IdentityChunker(),
        store=store,
    )
    stats = pipeline2.run(_ctx())
    assert stats.sources_skipped_unchanged == 0
    assert stats.sources_processed == 1
    assert store.chunks["mem:/a#0"].text == "second"
