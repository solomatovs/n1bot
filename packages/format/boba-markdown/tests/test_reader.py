"""MarkdownReader: markdown → типизированные Section'ы."""

from __future__ import annotations

from io import BytesIO

import pytest

# Skip if markdown-it-py not installed.
pytest.importorskip("markdown_it")

from boba.indexing import (
    HeadingSection,
    Metadata,
    ParagraphSection,
    RawDocument,
    ReaderId,
    ReaderKeys,
    SectionKeys,
    SourceId,
)
from boba.markdown import (
    MarkdownBlockquoteSection,
    MarkdownCodeFenceSection,
    MarkdownHorizontalRuleSection,
    MarkdownListSection,
    MarkdownReader,
    MarkdownTableSection,
)


def _doc(text: str, *, source_id: str = "doc1") -> RawDocument:
    return RawDocument(
        handle=BytesIO(text.encode("utf-8")),
        source_id=SourceId(source_id),
    )

def test_reader_id():
    assert MarkdownReader().reader_id() == ReaderId("ext.markdown")


def test_doc_type_in_metadata_of_each_section():
    md = "# A\n\npara"
    sections = list(MarkdownReader().convert(_doc(md)))
    for s in sections:
        assert s.metadata.get(ReaderKeys.DOC_TYPE) == "markdown"


def test_offset_invariant_holds_for_every_section():
    md = """# Title

intro paragraph.

```python
print("hi")
```

| a | b |
|---|---|
| 1 | 2 |

- alpha
- beta

> quote

---
"""
    for s in MarkdownReader().convert(_doc(md)):
        start = s.metadata.get(SectionKeys.LOCATION_START)
        end = s.metadata.get(SectionKeys.LOCATION_END)
        assert start is not None and end is not None
        assert md[start:end] == s.content


def test_empty_payload_yields_nothing():
    assert list(MarkdownReader().convert(_doc(""))) == []


def test_heading_section_typed_fields():
    md = "## My Section"
    [s] = list(MarkdownReader().convert(_doc(md)))
    assert isinstance(s, HeadingSection)
    assert s.level == 2
    assert s.text == "My Section"
    assert s.metadata.get(SectionKeys.ANCHOR) == "my-section"
    md_meta = s.to_chunk_metadata()
    assert md_meta.get(SectionKeys.HEADING_LEVEL) == 2
    assert md_meta.get(SectionKeys.HEADING_TEXT) == "My Section"


def test_multiple_headings_yield_separate_sections_in_order():
    md = "# h1\n\n## h2\n\n### h3"
    sections = [
        s for s in MarkdownReader().convert(_doc(md))
        if isinstance(s, HeadingSection)
    ]
    assert [(s.level, s.text) for s in sections] == [
        (1, "h1"), (2, "h2"), (3, "h3"),
    ]

def test_paragraph_keeps_inline_markdown():
    md = "This is **bold** and `code` and a [link](url)."
    [s] = list(MarkdownReader().convert(_doc(md)))
    assert isinstance(s, ParagraphSection)
    assert s.content == md



def test_code_fence_section_with_language():
    md = "```python\ndef f():\n    return 1\n```"
    [s] = list(MarkdownReader().convert(_doc(md)))
    assert isinstance(s, MarkdownCodeFenceSection)
    assert s.language == "python"
    assert s.code == "def f():\n    return 1\n"
    assert s.content == md
    assert len(s.code_line_locations) == 2


def test_code_fence_no_language():
    md = "```\nplain code\n```"
    [s] = list(MarkdownReader().convert(_doc(md)))
    assert isinstance(s, MarkdownCodeFenceSection)
    assert s.language is None



def test_table_section_typed_fields():
    md = "| name | type |\n|------|------|\n| id   | int  |\n| name | str  |"
    [s] = list(MarkdownReader().convert(_doc(md)))
    assert isinstance(s, MarkdownTableSection)
    assert s.header == ("name", "type")
    assert s.rows == (("id", "int"), ("name", "str"))
    assert s.content == md
    assert len(s.row_locations) == 2



def test_unordered_list_section():
    md = "- alpha\n- beta\n- gamma"
    [s] = list(MarkdownReader().convert(_doc(md)))
    assert isinstance(s, MarkdownListSection)
    assert s.ordered is False
    assert s.items == ("alpha", "beta", "gamma")
    assert len(s.item_locations) == 3


def test_ordered_list_section():
    md = "1. one\n2. two\n3. three"
    [s] = list(MarkdownReader().convert(_doc(md)))
    assert isinstance(s, MarkdownListSection)
    assert s.ordered is True



def test_blockquote_section():
    md = "> single line quote"
    [s] = list(MarkdownReader().convert(_doc(md)))
    assert isinstance(s, MarkdownBlockquoteSection)
    assert s.content == md



def test_horizontal_rule_section():
    md = "---"
    [s] = list(MarkdownReader().convert(_doc(md)))
    assert isinstance(s, MarkdownHorizontalRuleSection)

def test_blocks_emitted_in_document_order():
    md = "# A\n\npara\n\n```\ncode\n```\n\n- item"
    sections = list(MarkdownReader().convert(_doc(md)))
    types = [type(s).__name__ for s in sections]
    assert types == [
        "HeadingSection",
        "ParagraphSection",
        "MarkdownCodeFenceSection",
        "MarkdownListSection",
    ]

def test_metadata_merged_with_upstream():
    upstream = Metadata.from_wire({"source_url": "https://example.com/page"})
    raw = RawDocument(
        handle=BytesIO(b"# A\n\npara"),
        source_id=SourceId("doc1"),
        metadata=upstream,
    )
    sections = list(MarkdownReader().convert(raw))
    for s in sections:
        assert s.metadata.to_wire()["source_url"] == "https://example.com/page"
        assert s.metadata.get(ReaderKeys.DOC_TYPE) == "markdown"
