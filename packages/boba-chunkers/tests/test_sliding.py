"""SlidingChunker: per-section split с soft-break, стабильный chunk_id."""

from __future__ import annotations

from boba.chunkers.sliding import SlidingChunkerConfig, sliding_chunker
from boba.indexing import (
    ChunkerId,
    Metadata,
    ReaderKeys,
    Section,
    SourceId,
)
from boba.indexing.context import PipelineContext, PipelineId


def _ctx() -> PipelineContext:
    return PipelineContext(pipeline_id=PipelineId("t"))


def test_chunker_id():
    chunker = sliding_chunker(SlidingChunkerConfig())
    assert chunker.chunker_id() == ChunkerId("sliding")


def test_short_section_yields_single_chunk():
    chunker = sliding_chunker(SlidingChunkerConfig(chunk_size=100, chunk_overlap=10))
    sections = [Section(source_id=SourceId("x:/a"), content="hello world")]
    chunks = list(chunker.stream(_ctx(), iter(sections)))
    assert len(chunks) == 1
    assert chunks[0].content == "hello world"
    assert chunks[0].chunk_index == 0
    assert chunks[0].source_id.to_wire() == "x:/a"


def test_long_section_split_into_multiple_chunks():
    chunker = sliding_chunker(SlidingChunkerConfig(chunk_size=20, chunk_overlap=5))
    text = " ".join(["word"] * 50)  # ~ 250 chars
    sections = [Section(source_id=SourceId("x:/a"), content=text)]
    chunks = list(chunker.stream(_ctx(), iter(sections)))
    assert len(chunks) > 1
    assert all(c.source_id.to_wire() == "x:/a" for c in chunks)
    assert [c.chunk_index for c in chunks] == list(range(len(chunks)))


def test_chunk_id_stable_for_same_input():
    chunker1 = sliding_chunker(SlidingChunkerConfig(chunk_size=100, chunk_overlap=10))
    chunker2 = sliding_chunker(SlidingChunkerConfig(chunk_size=100, chunk_overlap=10))
    sections = [Section(source_id=SourceId("x:/a"), content="hello")]
    ids1 = [c.chunk_id for c in chunker1.stream(_ctx(), iter(sections))]
    ids2 = [c.chunk_id for c in chunker2.stream(_ctx(), iter(sections))]
    assert ids1 == ids2


def test_per_source_index_resets_across_sources():
    chunker = sliding_chunker(SlidingChunkerConfig(chunk_size=100, chunk_overlap=10))
    sections = [
        Section(source_id=SourceId("x:/a"), content="alpha"),
        Section(source_id=SourceId("x:/b"), content="beta"),
    ]
    chunks = list(chunker.stream(_ctx(), iter(sections)))
    by_src = {c.source_id.to_wire(): c.chunk_index for c in chunks}
    assert by_src == {"x:/a": 0, "x:/b": 0}


def test_anchor_and_metadata_propagate():
    chunker = sliding_chunker(SlidingChunkerConfig(chunk_size=100, chunk_overlap=10))
    md = Metadata.empty().set(ReaderKeys.DOC_TYPE, "md").set(
        ReaderKeys.PAGE_TITLE, "T"
    )
    sections = [
        Section(
            source_id=SourceId("x:/a"),
            content="hello",
            anchor="intro",
            metadata=md,
        )
    ]
    chunks = list(chunker.stream(_ctx(), iter(sections)))
    assert chunks[0].anchor == "intro"
    assert chunks[0].metadata.get(ReaderKeys.DOC_TYPE) == "md"
    assert chunks[0].metadata.get(ReaderKeys.PAGE_TITLE) == "T"
