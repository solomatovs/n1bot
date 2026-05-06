"""MarkdownReader: handle → heading-aware Section'ы."""

from __future__ import annotations

from io import BytesIO

from boba.processing import IndexingContext, PipelineId, RawDocument, ReaderId
from boba.markdown_reader import MarkdownReader


def _ctx() -> IndexingContext:
    return IndexingContext(pipeline_id=PipelineId("t"), collection="c")


def _doc(payload: str, *, source_id: str = "fs:/y", metadata=None) -> RawDocument:
    return RawDocument(
        handle=BytesIO(payload.encode("utf-8")),
        source_id=source_id,
        content_hint="md",
        metadata=metadata or {},
    )


def test_reader_id():
    assert MarkdownReader().reader_id() == ReaderId("ext.markdown")


def test_emits_one_section_for_single_heading():
    sections = list(MarkdownReader().convert(_ctx(), _doc("# Hello\n\nbody")))
    assert len(sections) == 1
    assert sections[0].text == "# Hello\n\nbody"
    assert sections[0].metadata["format"] == "markdown"


def test_heading_aware_split_per_heading():
    md = "# One\nalpha\n\n## Two\nbeta gamma\n"
    sections = list(MarkdownReader().convert(_ctx(), _doc(md)))
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
    sections = list(MarkdownReader().convert(_ctx(), _doc(md)))
    assert len(sections) == 2
    assert sections[0].anchor is None
    assert "intro line" in sections[0].text
    assert sections[1].anchor == "a"


def test_no_headings_yields_single_anchorless_section():
    sections = list(
        MarkdownReader().convert(_ctx(), _doc("just text without headings\nmore"))
    )
    assert len(sections) == 1
    assert sections[0].anchor is None


def test_anchor_slug_from_heading_text():
    sections = list(MarkdownReader().convert(_ctx(), _doc("## Hello, World!\nbody")))
    assert sections[0].anchor == "hello-world"
    assert sections[0].metadata["heading_level"] == "2"
    assert sections[0].metadata["heading_text"] == "Hello, World!"


def test_metadata_merge_from_raw_document():
    sections = list(
        MarkdownReader().convert(
            _ctx(),
            _doc("# A\nx", metadata={"upstream_field": "v"}),
        )
    )
    assert sections[0].metadata["upstream_field"] == "v"
    assert sections[0].metadata["format"] == "markdown"


def test_source_id_from_raw_document():
    sections = list(
        MarkdownReader().convert(_ctx(), _doc("# A\nbody", source_id="custom-id"))
    )
    assert all(s.source_id == "custom-id" for s in sections)


def test_code_fence_does_not_trigger_heading():
    md = "# Real\ntext\n```\n# not heading\n```\n## Real2\nbody"
    sections = list(MarkdownReader().convert(_ctx(), _doc(md)))
    headings = [s.anchor for s in sections if s.anchor]
    assert headings == ["real", "real2"]
