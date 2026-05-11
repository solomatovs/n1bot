"""to_format_plan() для всех Html*Section: markdown-render + per-chunk raw."""

from __future__ import annotations

from io import BytesIO

import pytest

from boba.html import (
    HtmlBlockquoteSection,
    HtmlCodeBlockSection,
    HtmlHorizontalRuleSection,
    HtmlListSection,
    HtmlParagraphSection,
    HtmlReader,
    HtmlTableSection,
)
from boba.indexing import (
    HeadingSection,
    Metadata,
    RawDocument,
    SourceId,
)


def _doc(html: str) -> RawDocument:
    return RawDocument(
        handle=BytesIO(html.encode("utf-8")),
        source_id=SourceId("fs:/x"),
        metadata=Metadata.empty(),
    )


def _section_by_type(html, cls):
    return next(s for s in HtmlReader().convert(_doc(html)) if isinstance(s, cls))


# ---------------- HeadingSection (домен) ---------------------------------------


def test_heading_format_plan_emits_markdown_and_breadcrumb():
    s = HeadingSection(
        source_id=SourceId("doc"),
        content="<h2>Auth</h2>",
        order=0,
        level=2,
        text="Auth",
    )
    plan = s.to_format_plan()
    assert plan.breadcrumb_level == 2
    assert plan.breadcrumb_text == "Auth"
    [block] = plan.blocks
    assert block.format_content == "## Auth"
    assert block.is_atomic is True


# ---------------- HtmlParagraphSection -----------------------------------------


def test_paragraph_format_plan_uses_text_for_embedder():
    """format_content — markdown/plain text; raw_content — оригинальный HTML."""
    html = "<html><body><p>hello <strong>world</strong></p></body></html>"
    s = _section_by_type(html, HtmlParagraphSection)
    [block] = s.to_format_plan().blocks
    assert "<p>" not in block.format_content
    assert "<strong>" not in block.format_content
    assert "hello" in block.format_content and "world" in block.format_content
    assert "<strong>" in block.raw_content


# ---------------- HtmlListSection ----------------------------------------------


def test_list_format_plan_per_item_atomic_blocks_with_raw():
    html = "<html><body><ul><li>a</li><li>b</li></ul></body></html>"
    s = _section_by_type(html, HtmlListSection)
    plan = s.to_format_plan()
    assert plan.block_glue == "\n"
    fmt = [b.format_content for b in plan.blocks]
    raw = [b.raw_content for b in plan.blocks]
    assert fmt == ["- a", "- b"]
    assert all(b.is_atomic for b in plan.blocks)
    assert all(r.startswith("<li>") for r in raw)


def test_ordered_list_uses_numbered_bullets():
    html = "<html><body><ol><li>x</li><li>y</li><li>z</li></ol></body></html>"
    s = _section_by_type(html, HtmlListSection)
    fmt = [b.format_content for b in s.to_format_plan().blocks]
    assert fmt == ["1. x", "2. y", "3. z"]


# ---------------- HtmlTableSection ---------------------------------------------


def test_table_format_plan_replicates_header_and_per_row_raw():
    html = (
        "<html><body><table>"
        "<thead><tr><th>name</th><th>type</th></tr></thead>"
        "<tbody>"
        "<tr><td>id</td><td>int</td></tr>"
        "<tr><td>foo</td><td>str</td></tr>"
        "</tbody></table></body></html>"
    )
    s = _section_by_type(html, HtmlTableSection)
    plan = s.to_format_plan()
    assert plan.repeat_header.startswith("| name | type |")
    assert "| --- | --- |" in plan.repeat_header
    fmt = [b.format_content for b in plan.blocks]
    assert fmt == ["| id | int |", "| foo | str |"]
    raw = [b.raw_content for b in plan.blocks]
    assert all(r.startswith("<tr>") for r in raw)
    # Каждый row — собственный raw, не общий HTML таблицы.
    assert raw[0] != raw[1]


def test_table_without_header_emits_empty_repeat_header():
    html = (
        "<html><body><table><tbody>"
        "<tr><td>a</td><td>b</td></tr>"
        "</tbody></table></body></html>"
    )
    s = _section_by_type(html, HtmlTableSection)
    plan = s.to_format_plan()
    assert plan.repeat_header == ""
    [block] = plan.blocks
    assert block.format_content == "| a | b |"


# ---------------- HtmlCodeBlockSection -----------------------------------------


def test_code_block_format_plan_wraps_in_fence():
    html = '<html><body><pre><code class="language-py">x = 1</code></pre></body></html>'
    s = _section_by_type(html, HtmlCodeBlockSection)
    plan = s.to_format_plan()
    assert plan.repeat_header == "```py\n"
    assert plan.repeat_footer == "\n```"
    [block] = plan.blocks
    assert block.format_content == "x = 1"
    assert block.is_atomic is False


def test_code_block_no_language_uses_empty_fence():
    html = "<html><body><pre><code>plain</code></pre></body></html>"
    s = _section_by_type(html, HtmlCodeBlockSection)
    plan = s.to_format_plan()
    assert plan.repeat_header == "```\n"


# ---------------- HtmlBlockquoteSection ----------------------------------------


def test_blockquote_format_plan_quotes_each_line():
    html = "<html><body><blockquote>quoted</blockquote></body></html>"
    s = _section_by_type(html, HtmlBlockquoteSection)
    [block] = s.to_format_plan().blocks
    assert block.format_content.startswith("> quoted")


# ---------------- HtmlHorizontalRuleSection ------------------------------------


def test_hr_format_plan_is_empty_so_chunker_skips():
    html = "<html><body><hr></body></html>"
    s = _section_by_type(html, HtmlHorizontalRuleSection)
    assert s.to_format_plan().blocks == ()


# ---------------- markdownify-specific (only when installed) -------------------


def test_paragraph_inline_markdown_when_markdownify_available():
    pytest.importorskip("markdownify")
    html = (
        '<html><body><p>see <a href="https://x.io">link</a> and '
        "<strong>bold</strong></p></body></html>"
    )
    s = _section_by_type(html, HtmlParagraphSection)
    [block] = s.to_format_plan().blocks
    assert "**bold**" in block.format_content
    assert "[link](https://x.io)" in block.format_content


# ---------------- HtmlPlainReader ----------------------------------------------


def test_plain_reader_separates_blocks_with_double_newline():
    from boba.html import HtmlPlainReader

    html = (
        "<html><head><title>T</title></head>"
        "<body><h1>H</h1><p>first</p><p>second</p></body></html>"
    )
    [s] = list(HtmlPlainReader().convert(_doc(html)))
    # Оба параграфа в content, разделены пустой строкой (а не пробелом).
    assert "first\n\nsecond" in s.content
