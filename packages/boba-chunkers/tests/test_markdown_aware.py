"""MarkdownAwareSplitter / markdown_aware_chunker: heading-priority резка."""

from __future__ import annotations

from boba.chunkers import (
    MarkdownAwareChunkerConfig,
    MarkdownAwareSplitter,
    markdown_aware_chunker,
)
from boba.indexing import (
    ChunkLocation,
    FixedDigestPrefix,
    Section,
    Sha256TextEncoder,
    SourceId,
    SplitPiece,
)
from boba.indexing.context import PipelineContext, PipelineId
from boba.indexing.section_chunker import SectionChunker


def _ctx() -> PipelineContext:
    return PipelineContext(pipeline_id=PipelineId("t"))


# ----------------------------- MarkdownAwareSplitter ----------------------------


def test_splitter_keeps_heading_marker_when_split_at_paragraph_break():
    """`\\n\\n` режет вокруг heading'а — маркер `## ` остаётся в чанке."""
    md = (
        "intro paragraph one\n"
        "intro paragraph two\n"
        "\n"
        "## Section\n"
        "section paragraph one\n"
        "section paragraph two"
    )
    pieces = list(MarkdownAwareSplitter(chunk_size=40, chunk_overlap=0).split(md))
    assert len(pieces) >= 2
    # Один из чанков должен начинаться с `## Section` — heading-маркер сохранён.
    assert any(p.content.lstrip().startswith("## Section") for p in pieces)


def test_splitter_inverse_invariant_over_join():
    """Для соседних pieces одного уровня: value[start:end] == content."""
    md = "alpha bravo charlie delta echo foxtrot golf hotel india"
    splitter = MarkdownAwareSplitter(chunk_size=12, chunk_overlap=0)
    for p in splitter.split(md):
        assert isinstance(p, SplitPiece)
        assert isinstance(p.location, ChunkLocation)
        assert md[p.location.start : p.location.end] == p.content


def test_splitter_keeps_codefence_intact_when_paragraph_break_around():
    """Code-fence окружён `\\n\\n` — не должен быть разрезан внутри."""
    md = (
        "intro\n"
        "\n"
        "```python\n"
        "def hello():\n"
        "    return 'world'\n"
        "```\n"
        "\n"
        "outro"
    )
    pieces = list(
        MarkdownAwareSplitter(chunk_size=80, chunk_overlap=0).split(md)
    )
    # Хотя бы один piece должен содержать целый code-fence (открытие+закрытие).
    fence_intact = any(
        "```python" in p.content and "```" in p.content.split("```python", 1)[1]
        for p in pieces
    )
    assert fence_intact


def test_splitter_empty_value_yields_nothing():
    assert list(MarkdownAwareSplitter(chunk_size=100).split("")) == []


def test_splitter_short_content_yields_single_piece():
    md = "# Tiny"
    pieces = list(MarkdownAwareSplitter(chunk_size=100, chunk_overlap=0).split(md))
    assert len(pieces) == 1
    assert pieces[0].content == md


# ----------------------------- markdown_aware_chunker ---------------------------


def _chunker(*, size: int = 1500, overlap: int = 0) -> SectionChunker:
    cfg = MarkdownAwareChunkerConfig(chunk_size=size, chunk_overlap=overlap)
    return markdown_aware_chunker(
        cfg,
        Sha256TextEncoder(),
        FixedDigestPrefix(cfg.digest_prefix_chars),
    )


def test_chunker_splits_long_markdown_section_with_anchor_preserved():
    md = (
        "first paragraph " * 10
        + "\n\n## Mid heading\n\n"
        + "second paragraph " * 10
    )
    sections = [
        Section(source_id=SourceId("x:/a"), content=md, anchor="big", order=0),
    ]
    chunks = list(_chunker(size=120, overlap=10).stream(_ctx(), iter(sections)))
    assert len(chunks) > 1
    assert all(c.anchor == "big" for c in chunks)


def test_chunker_short_section_yields_single_chunk():
    sections = [
        Section(
            source_id=SourceId("x:/a"),
            content="# Short\n\nbody",
            anchor="short",
            order=0,
        ),
    ]
    chunks = list(_chunker(size=1500).stream(_ctx(), iter(sections)))
    assert len(chunks) == 1
    assert chunks[0].content.startswith("# Short")
