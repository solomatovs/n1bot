"""MarkdownBlockParser: AST-парсер markdown'а через markdown-it-py."""

from __future__ import annotations

import pytest

# Skip if markdown-it-py not installed — это опциональная зависимость.
pytest.importorskip("markdown_it")

from boba.indexing import (
    BlockquoteBlock,
    CodeFenceBlock,
    HeadingBlock,
    HorizontalRuleBlock,
    ListBlock,
    ParagraphBlock,
    TableBlock,
)
from boba.markdown import MarkdownBlockParser


@pytest.fixture
def parser() -> MarkdownBlockParser:
    return MarkdownBlockParser()


def test_offset_invariant_holds_for_every_block_type(parser):
    """`original[location.start:location.end] == content` для каждого блока."""
    md = """# Title

intro paragraph.

```python
print("hi")
```

| a | b |
|---|---|
| 1 | 2 |

- item one
- item two

> quote text

---
"""
    blocks = parser.parse(md)
    assert len(blocks) == 7
    for b in blocks:
        sliced = md[b.location.start : b.location.end]
        assert sliced == b.content, f"invariant broken on {b}"


def test_heading_levels_extracted(parser):
    md = "# h1\n\n## h2\n\n### h3"
    blocks = parser.parse(md)
    headings = [b for b in blocks if isinstance(b, HeadingBlock)]
    assert [(h.level, h.text) for h in headings] == [(1, "h1"), (2, "h2"), (3, "h3")]


def test_heading_content_keeps_marker(parser):
    md = "## My Section"
    [block] = parser.parse(md)
    assert isinstance(block, HeadingBlock)
    assert block.content == "## My Section"
    assert block.level == 2
    assert block.text == "My Section"


def test_paragraph_keeps_inline_markdown_in_content(parser):
    md = "This is **bold** and `code` and a [link](url)."
    [block] = parser.parse(md)
    assert isinstance(block, ParagraphBlock)
    # Inline-разметка остаётся в content; парсер её не разворачивает.
    assert block.content == "This is **bold** and `code` and a [link](url)."


def test_code_fence_preserves_language_and_code(parser):
    md = '```python\ndef f():\n    return 1\n```'
    [block] = parser.parse(md)
    assert isinstance(block, CodeFenceBlock)
    assert block.language == "python"
    assert block.code == "def f():\n    return 1\n"
    # Content включает обрамляющие fence-маркеры — это slice исходника.
    assert block.content == md


def test_code_fence_no_language(parser):
    md = "```\nplain code\n```"
    [block] = parser.parse(md)
    assert isinstance(block, CodeFenceBlock)
    assert block.language is None
    assert block.code == "plain code\n"


def test_table_extracts_header_and_rows(parser):
    md = "| name | type |\n|------|------|\n| id   | int  |\n| name | str  |"
    [block] = parser.parse(md)
    assert isinstance(block, TableBlock)
    assert block.header == ("name", "type")
    assert block.rows == (("id", "int"), ("name", "str"))
    # Content — оригинальный slice без нормализации.
    assert block.content == md


def test_unordered_list_items(parser):
    md = "- alpha\n- beta\n- gamma"
    [block] = parser.parse(md)
    assert isinstance(block, ListBlock)
    assert block.ordered is False
    assert block.items == ("alpha", "beta", "gamma")


def test_ordered_list_items(parser):
    md = "1. one\n2. two\n3. three"
    [block] = parser.parse(md)
    assert isinstance(block, ListBlock)
    assert block.ordered is True
    assert block.items == ("one", "two", "three")


def test_blockquote_content(parser):
    md = "> single line quote"
    [block] = parser.parse(md)
    assert isinstance(block, BlockquoteBlock)
    assert block.content == "> single line quote"


def test_horizontal_rule(parser):
    md = "---"
    [block] = parser.parse(md)
    assert isinstance(block, HorizontalRuleBlock)
    assert block.content == "---"

def test_blocks_emitted_in_document_order(parser):
    md = "# A\n\npara\n\n```\ncode\n```\n\n- item"
    blocks = parser.parse(md)
    types = [type(b).__name__ for b in blocks]
    assert types == ["HeadingBlock", "ParagraphBlock", "CodeFenceBlock", "ListBlock"]


def test_empty_text_returns_empty_list(parser):
    assert parser.parse("") == []
