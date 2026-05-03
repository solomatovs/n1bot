"""TextReader: accepts txt/text/log, decodes UTF-8, выдаёт одну Section."""

from __future__ import annotations

from boba.ext.text_reader.reader import TextReader
from boba.indexing import IndexingContext, PipelineId, ReaderId, SourceItem


def _ctx() -> IndexingContext:
    return IndexingContext(pipeline_id=PipelineId("t"), collection="c")


def _item(hint: str, payload: str) -> SourceItem:
    return SourceItem(
        source_id="x:/y", content_hint=hint, payload=payload.encode("utf-8")
    )


def test_reader_id():
    assert TextReader().reader_id() == ReaderId("ext.text")


def test_accepts_txt_text_log():
    r = TextReader()
    assert r.accepts(_item("txt", "x"))
    assert r.accepts(_item("text", "x"))
    assert r.accepts(_item("log", "x"))


def test_rejects_other_hints():
    r = TextReader()
    assert not r.accepts(_item("md", "x"))
    assert not r.accepts(_item("html", "x"))


def test_emits_one_section_with_text():
    r = TextReader()
    sections = list(r.convert(_ctx(), _item("txt", "hello world")))
    assert len(sections) == 1
    assert sections[0].text == "hello world"
    assert sections[0].metadata["format"] == "text"
    assert sections[0].source_id == "x:/y"


def test_skips_empty_payload():
    r = TextReader()
    assert list(r.convert(_ctx(), _item("txt", "   "))) == []
