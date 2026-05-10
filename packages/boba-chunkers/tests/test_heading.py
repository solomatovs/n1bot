"""HeadingChunker: anchor preserved, no cross-section bleed."""

from __future__ import annotations

from boba.chunkers.heading import HeadingChunkerConfig, heading_chunker
from boba.indexing import (
    FixedDigestPrefix,
    Metadata,
    ReaderKeys,
    Section,
    Sha256TextEncoder,
    SourceId,
)
from boba.indexing.context import PipelineContext, PipelineId
from boba.indexing.section_chunker import SectionChunker


def _ctx() -> PipelineContext:
    return PipelineContext(pipeline_id=PipelineId("t"))


def _chunker(*, size: int = 1500, overlap: int = 0) -> SectionChunker:
    cfg = HeadingChunkerConfig(chunk_size=size, chunk_overlap=overlap)
    return heading_chunker(
        cfg,
        Sha256TextEncoder(),
        FixedDigestPrefix(cfg.digest_prefix_chars),
    )


def test_long_section_split_with_overlap_keeps_anchor():
    long_text = " ".join(["paragraph"] * 200)  # ≈ 1800 chars
    sections = [
        Section(source_id=SourceId("x:/a"), content=long_text, anchor="big", order=0),
    ]
    chunks = list(_chunker(size=200, overlap=20).stream(_ctx(), iter(sections)))
    assert len(chunks) > 1
    assert all(c.anchor == "big" for c in chunks)


def test_no_cross_section_bleed():
    """Чанки разных Section'ов не должны содержать текст другой Section."""
    sections = [
        Section(source_id=SourceId("x:/a"), content="alpha alpha alpha", anchor="one"),
        Section(source_id=SourceId("x:/a"), content="beta beta beta", anchor="two"),
    ]
    chunks = list(_chunker(size=20, overlap=0).stream(_ctx(), iter(sections)))
    by_anchor = {c.anchor: c.content for c in chunks}
    assert "beta" not in by_anchor["one"]
    assert "alpha" not in by_anchor["two"]


def test_section_without_anchor_passes_none():
    sections = [Section(source_id=SourceId("x:/a"), content="plain text")]
    chunks = list(_chunker().stream(_ctx(), iter(sections)))
    assert chunks[0].anchor is None


def test_chunk_id_stable_for_same_input():
    sections = [Section(source_id=SourceId("x:/a"), content="hello", anchor="intro")]
    ids1 = [c.chunk_id for c in _chunker().stream(_ctx(), iter(sections))]
    ids2 = [c.chunk_id for c in _chunker().stream(_ctx(), iter(sections))]
    assert ids1 == ids2


def test_per_source_chunk_index_runs_through_sections():
    """Один source_id с несколькими Section'ами получает сквозную нумерацию."""
    sections = [
        Section(source_id=SourceId("x:/a"), content="a1", anchor="one"),
        Section(source_id=SourceId("x:/a"), content="b2", anchor="two"),
        Section(source_id=SourceId("x:/b"), content="c3", anchor="one"),
    ]
    chunks = list(_chunker().stream(_ctx(), iter(sections)))
    by_src = [(c.source_id.to_wire(), c.chunk_index, c.anchor) for c in chunks]
    assert by_src == [
        ("x:/a", 0, "one"),
        ("x:/a", 1, "two"),
        ("x:/b", 0, "one"),
    ]


def test_metadata_propagates_from_section():
    md = Metadata.empty().set(ReaderKeys.DOC_TYPE, "confluence_html").set(
        ReaderKeys.PAGE_TITLE, "Doc"
    )
    sections = [
        Section(
            source_id=SourceId("x:/a"),
            content="hello",
            anchor="intro",
            metadata=md,
        )
    ]
    chunks = list(_chunker().stream(_ctx(), iter(sections)))
    assert chunks[0].metadata.get(ReaderKeys.DOC_TYPE) == "confluence_html"
    assert chunks[0].metadata.get(ReaderKeys.PAGE_TITLE) == "Doc"
