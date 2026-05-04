"""MarkdownReader: accepts md/markdown, выдаёт plain-text Section."""

from __future__ import annotations

from boba.ext.markdown_reader.reader import MarkdownReader
from boba.indexing import IndexingContext, PipelineId, ReaderId, SourceItem


def _ctx() -> IndexingContext:
    return IndexingContext(pipeline_id=PipelineId("t"), collection="c")


def _item(hint: str, payload: str) -> SourceItem:
    return SourceItem(
        source_id="x:/y", content_hint=hint, payload=payload.encode("utf-8")
    )


def test_reader_id():
    assert MarkdownReader().reader_id() == ReaderId("ext.markdown")


def test_accepts_md_and_markdown():
    r = MarkdownReader()
    assert r.accepts(_item("md", "# x"))
    assert r.accepts(_item("markdown", "# x"))


def test_rejects_other_hints():
    r = MarkdownReader()
    assert not r.accepts(_item("txt", "x"))
    assert not r.accepts(_item("html", "x"))


def test_emits_one_section_with_format_markdown():
    sections = list(
        MarkdownReader().convert(_ctx(), _item("md", "# Hello\n\nbody"))
    )
    assert len(sections) == 1
    assert sections[0].text == "# Hello\n\nbody"
    assert sections[0].metadata["format"] == "markdown"


def test_heading_aware_split_per_heading():
    md = "# One\nalpha\n\n## Two\nbeta gamma\n"
    sections = list(MarkdownReader().convert(_ctx(), _item("md", md)))
    assert len(sections) == 2
    assert sections[0].anchor == "one"
    assert "One" in sections[0].text
    assert "alpha" in sections[0].text
    assert "beta" not in sections[0].text  # не утёк во вторую
    assert sections[1].anchor == "two"
    assert "Two" in sections[1].text
    assert "beta gamma" in sections[1].text


def test_preamble_becomes_anchorless_section():
    md = "intro line\n\n# A\nbody\n"
    sections = list(MarkdownReader().convert(_ctx(), _item("md", md)))
    assert len(sections) == 2
    assert sections[0].anchor is None
    assert "intro line" in sections[0].text
    assert sections[1].anchor == "a"


def test_no_headings_yields_single_anchorless_section():
    md = "just text without headings\nmore text"
    sections = list(MarkdownReader().convert(_ctx(), _item("md", md)))
    assert len(sections) == 1
    assert sections[0].anchor is None
    assert "just text" in sections[0].text


def test_anchor_slug_from_heading_text():
    md = "## Hello, World!\nbody"
    sections = list(MarkdownReader().convert(_ctx(), _item("md", md)))
    assert sections[0].anchor == "hello-world"
    assert sections[0].metadata["heading_level"] == "2"
    assert sections[0].metadata["heading_text"] == "Hello, World!"


def test_content_hash_propagated_to_each_section():
    md = "# A\nx\n## B\ny\n"
    item = SourceItem(
        source_id="x:/y", content_hint="md",
        payload=md.encode("utf-8"), content_hash="v42",
    )
    sections = list(MarkdownReader().convert(_ctx(), item))
    assert all(s.content_hash == "v42" for s in sections)


def test_code_fence_does_not_trigger_heading():
    md = "# Real\ntext\n```\n# not heading\n```\n## Real2\nbody"
    sections = list(MarkdownReader().convert(_ctx(), _item("md", md)))
    headings = [s.anchor for s in sections if s.anchor]
    assert headings == ["real", "real2"]
