"""HeadingChunker: anchor preserved, no cross-section bleed."""

from __future__ import annotations

from boba.chunking.heading import HeadingChunkerConfig, heading_chunker
from boba.indexing import ChunkerId
from boba.indexing.section_chunker import SectionChunker
from boba.processing import IndexingContext, PipelineId, Section


def _ctx() -> IndexingContext:
    return IndexingContext(pipeline_id=PipelineId("t"), collection="c")


def _chunker(*, size: int = 1500, overlap: int = 0) -> SectionChunker:
    return heading_chunker(
        HeadingChunkerConfig(chunk_size=size, chunk_overlap=overlap)
    )


def test_chunker_id():
    assert _chunker().chunker_id() == ChunkerId("ext.heading")


def test_short_section_yields_one_chunk_with_anchor():
    sections = [
        Section(source_id="x:/a", text="hello", anchor="intro", order=0),
    ]
    chunks = list(_chunker().stream(_ctx(), iter(sections)))
    assert len(chunks) == 1
    assert chunks[0].anchor == "intro"
    assert chunks[0].text == "hello"
    assert chunks[0].chunk_index == 0


def test_long_section_split_with_overlap_keeps_anchor():
    long_text = " ".join(["paragraph"] * 200)  # ≈ 1800 chars
    sections = [
        Section(source_id="x:/a", text=long_text, anchor="big", order=0),
    ]
    chunks = list(_chunker(size=200, overlap=20).stream(_ctx(), iter(sections)))
    assert len(chunks) > 1
    assert all(c.anchor == "big" for c in chunks)


def test_no_cross_section_bleed():
    """Чанки разных Section'ов не должны содержать текст другой Section."""
    sections = [
        Section(source_id="x:/a", text="alpha alpha alpha", anchor="one"),
        Section(source_id="x:/a", text="beta beta beta", anchor="two"),
    ]
    chunks = list(_chunker(size=20, overlap=0).stream(_ctx(), iter(sections)))
    by_anchor = {c.anchor: c.text for c in chunks}
    assert "beta" not in by_anchor["one"]
    assert "alpha" not in by_anchor["two"]


def test_section_without_anchor_passes_none():
    sections = [Section(source_id="x:/a", text="plain text")]
    chunks = list(_chunker().stream(_ctx(), iter(sections)))
    assert chunks[0].anchor is None


def test_chunk_id_stable_for_same_input():
    sections = [Section(source_id="x:/a", text="hello", anchor="intro")]
    ids1 = [c.chunk_id for c in _chunker().stream(_ctx(), iter(sections))]
    ids2 = [c.chunk_id for c in _chunker().stream(_ctx(), iter(sections))]
    assert ids1 == ids2


def test_per_source_chunk_index_runs_through_sections():
    """Один source_id с несколькими Section'ами получает сквозную нумерацию."""
    sections = [
        Section(source_id="x:/a", text="a1", anchor="one"),
        Section(source_id="x:/a", text="b2", anchor="two"),
        Section(source_id="x:/b", text="c3", anchor="one"),
    ]
    chunks = list(_chunker().stream(_ctx(), iter(sections)))
    by_src = [(c.source_id, c.chunk_index, c.anchor) for c in chunks]
    assert by_src == [
        ("x:/a", 0, "one"),
        ("x:/a", 1, "two"),
        ("x:/b", 0, "one"),
    ]


def test_metadata_propagates_from_section():
    sections = [
        Section(
            source_id="x:/a",
            text="hello",
            anchor="intro",
            metadata={"format": "confluence_html", "title": "Doc"},
        )
    ]
    chunks = list(_chunker().stream(_ctx(), iter(sections)))
    assert chunks[0].metadata["format"] == "confluence_html"
    assert chunks[0].metadata["title"] == "Doc"
