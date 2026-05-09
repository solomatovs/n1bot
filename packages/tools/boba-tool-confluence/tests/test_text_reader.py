"""TextReader: handle → одна Section с metadata-merge."""

from __future__ import annotations

from io import BytesIO

from boba.fs_pipelines.text_reader import TextReader
from boba.processing import PipelineContext, PipelineId, RawDocument, ReaderId


def _ctx() -> PipelineContext:
    return PipelineContext(pipeline_id=PipelineId("t"), collection="c")


def _doc(payload: str, *, source_id: str = "fs:/y", metadata=None) -> RawDocument:
    return RawDocument(
        handle=BytesIO(payload.encode("utf-8")),
        source_id=source_id,
        content_hint="txt",
        metadata=metadata or {},
    )


def test_reader_id():
    assert TextReader().reader_id() == ReaderId("ext.text")


def test_emits_one_section():
    sections = list(TextReader().convert(_doc("hello world")))
    assert len(sections) == 1
    assert sections[0].text == "hello world"
    assert sections[0].metadata["format"] == "text"


def test_skips_empty_payload():
    assert list(TextReader().convert(_doc("   "))) == []


def test_metadata_merge_from_raw_document():
    """Reader должен мерж'ить свои поля с RawDocument.metadata."""
    sections = list(
        TextReader().convert(
            _doc(
                "x",
                metadata={"my_upstream_field": "value", "format": "OVERRIDE"},
            ),
        )
    )
    assert sections[0].metadata["my_upstream_field"] == "value"
    # Reader пишет 'format' поверх — это ожидаемо
    assert sections[0].metadata["format"] == "text"


def test_source_id_from_raw_document():
    sections = list(TextReader().convert(_doc("x", source_id="fs:/abs/y")))
    assert sections[0].source_id == "fs:/abs/y"


def test_invalid_utf8_replaced_not_raised():
    bad = b"\xff\xfehello"
    doc = RawDocument(
        handle=BytesIO(bad), source_id="fs:/y", content_hint="txt", metadata={}
    )
    sections = list(TextReader().convert(doc))
    assert len(sections) == 1
    assert "hello" in sections[0].text  # утончения через errors='replace'
