"""HTML reader'ы: structural / plain / readability."""

from __future__ import annotations

from collections.abc import Callable

import pytest

from boba.html import (
    HtmlBlockquoteSection,
    HtmlCodeBlockSection,
    HtmlHorizontalRuleSection,
    HtmlKeys,
    HtmlListSection,
    HtmlPlainReader,
    HtmlReadabilityReader,
    HtmlReader,
    HtmlTableSection,
)
from boba.indexing import (
    HeadingSection,
    Metadata,
    ParagraphSection,
    RawDocument,
    ReaderId,
    ReaderKeys,
    SectionKeys,
)

_MakeDoc = Callable[..., RawDocument]


# ------------------------------ HtmlReader -------------------------------------


def test_reader_id():
    assert HtmlReader().reader_id() == ReaderId("ext.html")


def test_reader_emits_typed_sections_in_document_order(make_raw_doc: _MakeDoc):
    """Все 7 типов в правильном порядке."""
    html = (
        "<html><body>\n"
        '<h1 id="intro">Intro</h1>\n'
        "<p>para text</p>\n"
        "<ul>\n  <li>a</li>\n  <li>b</li>\n</ul>\n"
        "<table>\n"
        "  <thead><tr><th>name</th><th>type</th></tr></thead>\n"
        "  <tbody>\n    <tr><td>id</td><td>int</td></tr>\n  </tbody>\n"
        "</table>\n"
        '<pre><code class="language-py">x = 1</code></pre>\n'
        "<blockquote>quote</blockquote>\n"
        "<hr>\n"
        "</body></html>"
    )
    sections = list(HtmlReader().convert(make_raw_doc(html)))
    types = [type(s).__name__ for s in sections]
    assert types == [
        "HeadingSection",
        "HtmlParagraphSection",
        "HtmlListSection",
        "HtmlTableSection",
        "HtmlCodeBlockSection",
        "HtmlBlockquoteSection",
        "HtmlHorizontalRuleSection",
    ]


def test_heading_section_has_typed_fields_and_anchor(make_raw_doc: _MakeDoc):
    html = '<html><body><h1 id="intro">Intro</h1></body></html>'
    [s] = list(HtmlReader().convert(make_raw_doc(html)))
    assert isinstance(s, HeadingSection)
    assert s.level == 1
    assert s.text == "Intro"
    assert s.metadata.get(SectionKeys.ANCHOR) == "intro"
    md = s.to_chunk_metadata()
    assert md.get(SectionKeys.HEADING_LEVEL) == 1
    assert md.get(SectionKeys.HEADING_TEXT) == "Intro"


def test_heading_anchor_falls_back_to_slug(make_raw_doc: _MakeDoc):
    html = "<html><body><h2>My Section</h2></body></html>"
    [s] = list(HtmlReader().convert(make_raw_doc(html)))
    assert isinstance(s, HeadingSection)
    assert s.metadata.get(SectionKeys.ANCHOR) == "my-section"


def test_list_section_typed_fields(make_raw_doc: _MakeDoc):
    html = "<html><body><ol><li>one</li><li>two</li><li>three</li></ol></body></html>"
    [s] = list(HtmlReader().convert(make_raw_doc(html)))
    assert isinstance(s, HtmlListSection)
    assert s.ordered is True
    assert s.items == ("one", "two", "three")


def test_unordered_list_section(make_raw_doc: _MakeDoc):
    html = "<html><body><ul><li>a</li><li>b</li></ul></body></html>"
    [s] = list(HtmlReader().convert(make_raw_doc(html)))
    assert isinstance(s, HtmlListSection)
    assert s.ordered is False
    md = s.to_chunk_metadata()
    assert md.get(HtmlKeys.LIST_ORDERED) is False


def test_table_section_typed_fields(make_raw_doc: _MakeDoc):
    html = (
        "<html><body><table>"
        "<thead><tr><th>name</th><th>type</th></tr></thead>"
        "<tbody>"
        "<tr><td>id</td><td>int</td></tr>"
        "<tr><td>foo</td><td>str</td></tr>"
        "</tbody>"
        "</table></body></html>"
    )
    [s] = list(HtmlReader().convert(make_raw_doc(html)))
    assert isinstance(s, HtmlTableSection)
    assert s.header == ("name", "type")
    assert s.rows == (("id", "int"), ("foo", "str"))


def test_code_block_language_from_class(make_raw_doc: _MakeDoc):
    html = (
        '<html><body><pre><code class="language-py">def hi(): pass</code></pre>'
        "</body></html>"
    )
    [s] = list(HtmlReader().convert(make_raw_doc(html)))
    assert isinstance(s, HtmlCodeBlockSection)
    assert s.language == "py"
    assert "def hi()" in s.code


def test_code_block_lang_prefix_also_recognized(make_raw_doc: _MakeDoc):
    html = (
        '<html><body><pre><code class="lang-rust">fn x() {}</code></pre>'
        "</body></html>"
    )
    [s] = list(HtmlReader().convert(make_raw_doc(html)))
    assert isinstance(s, HtmlCodeBlockSection)
    assert s.language == "rust"


def test_code_block_no_language(make_raw_doc: _MakeDoc):
    html = "<html><body><pre><code>plain</code></pre></body></html>"
    [s] = list(HtmlReader().convert(make_raw_doc(html)))
    assert isinstance(s, HtmlCodeBlockSection)
    assert s.language is None


def test_blockquote_section(make_raw_doc: _MakeDoc):
    html = "<html><body><blockquote>quoted</blockquote></body></html>"
    [s] = list(HtmlReader().convert(make_raw_doc(html)))
    assert isinstance(s, HtmlBlockquoteSection)
    assert "quoted" in s.content


def test_hr_section(make_raw_doc: _MakeDoc):
    html = "<html><body><hr></body></html>"
    [s] = list(HtmlReader().convert(make_raw_doc(html)))
    assert isinstance(s, HtmlHorizontalRuleSection)


def test_wrapper_tags_are_transparent(make_raw_doc: _MakeDoc):
    """`<div>`/`<section>`/`<article>` обходятся транзитивно."""
    html = (
        "<html><body>"
        "<div><h1>One</h1></div>"
        "<section><p>para</p></section>"
        "<article><ul><li>x</li></ul></article>"
        "</body></html>"
    )
    sections = list(HtmlReader().convert(make_raw_doc(html)))
    types = [type(s).__name__ for s in sections]
    assert types == ["HeadingSection", "HtmlParagraphSection", "HtmlListSection"]


def test_script_and_style_are_dropped(make_raw_doc: _MakeDoc):
    html = (
        "<html><body>"
        "<script>alert('x')</script>"
        "<h1>OK</h1>"
        "<style>h1{}</style>"
        "<p>body</p>"
        "</body></html>"
    )
    sections = list(HtmlReader().convert(make_raw_doc(html)))
    combined = "\n".join(s.content for s in sections)
    assert "alert" not in combined
    assert "h1{}" not in combined
    assert "OK" in combined
    assert "body" in combined


def test_location_invariant_holds_per_section(make_raw_doc: _MakeDoc):
    """`text[loc.start:loc.end]` покрывает HTML-разметку секции."""
    html = (
        "<html><body>\n"
        "<h1>Title</h1>\n"
        "<p>para</p>\n"
        "</body></html>"
    )
    text = html
    for s in HtmlReader().convert(make_raw_doc(html)):
        start = s.metadata.get(SectionKeys.LOCATION_START)
        end = s.metadata.get(SectionKeys.LOCATION_END)
        assert start is not None and end is not None
        # line-precision: start ≤ end ≤ len(text); содержание строки попадает в slice
        assert 0 <= start <= end <= len(text)


def test_metadata_merge_from_raw_document(make_raw_doc: _MakeDoc):
    html = "<html><body><h1>A</h1><p>x</p></body></html>"
    upstream = Metadata.from_wire({"source_url": "https://example.com/page"})
    sections = list(HtmlReader().convert(make_raw_doc(html, metadata=upstream)))
    for s in sections:
        assert (
            s.metadata.to_wire()["source_url"] == "https://example.com/page"
        )
        assert s.metadata.get(ReaderKeys.DOC_TYPE) == "html"


def test_title_in_metadata(make_raw_doc: _MakeDoc):
    html = (
        "<html><head><title>Page</title></head>"
        "<body><h1>X</h1></body></html>"
    )
    sections = list(HtmlReader().convert(make_raw_doc(html)))
    for s in sections:
        assert s.metadata.get(ReaderKeys.PAGE_TITLE) == "Page"


def test_empty_payload_yields_nothing(make_raw_doc: _MakeDoc):
    assert list(HtmlReader().convert(make_raw_doc(""))) == []


# ------------------------------ HtmlPlainReader --------------------------------


def test_plain_reader_id():
    assert HtmlPlainReader().reader_id() == ReaderId("ext.html.plain")


def test_plain_yields_single_section_with_full_body(make_raw_doc: _MakeDoc):
    html = (
        "<html><head><title>Page</title></head>"
        "<body><h1>X</h1><p>first</p><p>second</p></body></html>"
    )
    sections = list(HtmlPlainReader().convert(make_raw_doc(html)))
    assert len(sections) == 1
    s = sections[0]
    assert isinstance(s, ParagraphSection)
    assert s.metadata.get(SectionKeys.ANCHOR) is None
    assert s.order == 0
    assert "Page" in s.content
    assert "X" in s.content
    assert "first" in s.content
    assert "second" in s.content
    assert s.metadata.get(ReaderKeys.PAGE_TITLE) == "Page"


def test_plain_drops_script_and_style(make_raw_doc: _MakeDoc):
    html = (
        "<html><body><script>alert('x')</script>"
        "<style>h1{}</style><p>real</p></body></html>"
    )
    sections = list(HtmlPlainReader().convert(make_raw_doc(html)))
    assert "alert" not in sections[0].content
    assert "h1{}" not in sections[0].content
    assert "real" in sections[0].content


def test_plain_empty_payload_yields_nothing(make_raw_doc: _MakeDoc):
    assert list(HtmlPlainReader().convert(make_raw_doc(""))) == []


# ----------------------------- HtmlReadabilityReader ---------------------------


# Skip if trafilatura not installed.
pytest.importorskip("trafilatura")


def test_readability_reader_id():
    assert HtmlReadabilityReader().reader_id() == ReaderId("ext.html.readability")


def test_readability_extracts_main_content_skipping_boilerplate(
    make_raw_doc: _MakeDoc,
):
    html = (
        "<html><head><title>News page</title></head><body>"
        "<nav>Menu | Home | About</nav>"
        "<article><h1>Important News</h1>"
        "<p>This is the main story body that has enough text to be extracted.</p>"
        "<p>A second paragraph with more substantive content for the article.</p>"
        "</article>"
        "<footer>(c) 2026 example.com</footer>"
        "</body></html>"
    )
    sections = list(HtmlReadabilityReader().convert(make_raw_doc(html)))
    assert len(sections) == 1
    s = sections[0]
    assert "main story" in s.content
    assert "Menu" not in s.content
    assert "(c) 2026" not in s.content
    assert s.metadata.get(SectionKeys.ANCHOR) is None


def test_readability_empty_payload_yields_nothing(make_raw_doc: _MakeDoc):
    assert list(HtmlReadabilityReader().convert(make_raw_doc(""))) == []
