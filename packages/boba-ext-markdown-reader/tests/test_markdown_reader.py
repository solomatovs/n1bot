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
