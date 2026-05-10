"""HTML reader'ы: heading / plain / semantic / readability."""

from __future__ import annotations

from io import BytesIO

import pytest

from boba.indexing import (
    Metadata,
    RawDocument,
    ReaderId,
    ReaderKeys,
    SourceId,
)
from boba.html import (
    HtmlHeadingReader,
    HtmlKeys,
    HtmlPlainReader,
    HtmlReadabilityReader,
    HtmlSemanticReader,
)


def _doc(
    html: str,
    *,
    source_id: str = "fs:/x",
    metadata: Metadata | None = None,
) -> RawDocument:
    return RawDocument(
        handle=BytesIO(html.encode("utf-8")),
        source_id=SourceId(source_id),
        metadata=metadata or Metadata.empty(),
    )


# ------------------------------ HtmlHeadingReader ------------------------------


def test_heading_reader_id():
    assert HtmlHeadingReader().reader_id() == ReaderId("ext.html.heading")


def test_heading_yields_separate_sections():
    html = """
    <html><body>
      <h1>One</h1><p>alpha</p>
      <h2>Two</h2><p>beta gamma</p>
    </body></html>
    """
    sections = list(HtmlHeadingReader().convert(_doc(html)))
    assert len(sections) == 2
    assert sections[0].anchor == "idx:1"
    assert "One" in sections[0].content
    assert "alpha" in sections[0].content
    assert "beta" not in sections[0].content
    assert sections[1].anchor == "idx:2"
    assert "Two" in sections[1].content
    assert "beta gamma" in sections[1].content


def test_heading_anchor_uses_html_id_when_present():
    html = '<html><body><h1 id="intro">Intro</h1><p>x</p></body></html>'
    sections = list(HtmlHeadingReader().convert(_doc(html)))
    assert sections[0].anchor == "intro"


def test_heading_no_headings_falls_back_to_single_section_with_title():
    html = (
        "<html><head><title>Doc Title</title></head>"
        "<body><p>just paragraphs</p></body></html>"
    )
    sections = list(HtmlHeadingReader().convert(_doc(html)))
    assert len(sections) == 1
    assert sections[0].anchor is None
    assert "Doc Title" in sections[0].content
    assert "just paragraphs" in sections[0].content


def test_heading_empty_headings_skipped():
    """Heading'и из одних картинок (без текста) — пропускаются."""
    html = """
    <html><body>
      <p>real content</p>
      <h1><img src="prev.png"/></h1>
      <h1><img src="home.png"/></h1>
    </body></html>
    """
    sections = list(HtmlHeadingReader().convert(_doc(html)))
    assert len(sections) == 1
    assert sections[0].anchor is None
    assert "real content" in sections[0].content


def test_heading_script_and_style_dropped():
    html = (
        "<html><body><script>alert('x')</script>"
        "<h1>OK</h1><style>h1{}</style><p>body</p></body></html>"
    )
    sections = list(HtmlHeadingReader().convert(_doc(html)))
    assert "alert" not in sections[0].content
    assert "h1{}" not in sections[0].content
    assert "OK" in sections[0].content
    assert "body" in sections[0].content


def test_heading_metadata_merge_from_raw_document():
    """upstream metadata (например source_url) пробрасывается в Section."""
    html = "<html><body><h1>A</h1><p>x</p></body></html>"
    upstream = Metadata.from_wire({"source_url": "https://example.com/page"})
    sections = list(HtmlHeadingReader().convert(_doc(html, metadata=upstream)))
    assert (
        sections[0].metadata.to_wire()["source_url"]
        == "https://example.com/page"
    )
    assert sections[0].metadata.get(HtmlKeys.HEADING_TEXT) == "A"


def test_heading_empty_payload_yields_nothing():
    sections = list(HtmlHeadingReader().convert(_doc("")))
    assert sections == []


# ------------------------------ HtmlPlainReader --------------------------------


def test_plain_reader_id():
    assert HtmlPlainReader().reader_id() == ReaderId("ext.html.plain")


def test_plain_yields_single_section_with_full_body():
    html = (
        "<html><head><title>Page</title></head>"
        "<body><h1>X</h1><p>first</p><p>second</p></body></html>"
    )
    sections = list(HtmlPlainReader().convert(_doc(html)))
    assert len(sections) == 1
    s = sections[0]
    assert s.anchor is None
    assert s.order == 0
    assert "Page" in s.content
    assert "X" in s.content
    assert "first" in s.content
    assert "second" in s.content
    assert s.metadata.get(ReaderKeys.PAGE_TITLE) == "Page"


def test_plain_drops_script_and_style():
    html = (
        "<html><body><script>alert('x')</script>"
        "<style>h1{}</style><p>real</p></body></html>"
    )
    sections = list(HtmlPlainReader().convert(_doc(html)))
    assert "alert" not in sections[0].content
    assert "h1{}" not in sections[0].content
    assert "real" in sections[0].content


def test_plain_empty_payload_yields_nothing():
    assert list(HtmlPlainReader().convert(_doc(""))) == []


# ------------------------------ HtmlSemanticReader -----------------------------


def test_semantic_reader_id():
    assert HtmlSemanticReader().reader_id() == ReaderId("ext.html.semantic")


def test_semantic_yields_section_per_top_level_block():
    html = (
        "<html><body>"
        "<article id='post-1'><h2>Post 1</h2><p>alpha</p></article>"
        "<article id='post-2'><h2>Post 2</h2><p>beta</p></article>"
        "</body></html>"
    )
    sections = list(HtmlSemanticReader().convert(_doc(html)))
    assert len(sections) == 2
    assert sections[0].anchor == "post-1"
    assert "Post 1" in sections[0].content
    assert "alpha" in sections[0].content
    assert sections[1].anchor == "post-2"
    assert "Post 2" in sections[1].content
    assert "beta" in sections[1].content


def test_semantic_skips_nested_blocks_to_avoid_duplicates():
    """Внутреннюю <section> внутри <article> не дублирует — берёт только outer."""
    html = (
        "<html><body>"
        "<article id='a'>"
        "  <section id='inner'><p>inner text</p></section>"
        "</article>"
        "</body></html>"
    )
    sections = list(HtmlSemanticReader().convert(_doc(html)))
    assert len(sections) == 1
    assert sections[0].anchor == "a"
    assert "inner text" in sections[0].content


def test_semantic_anchor_falls_back_to_idx_when_no_id():
    html = (
        "<html><body>"
        "<article><p>first</p></article>"
        "<article><p>second</p></article>"
        "</body></html>"
    )
    sections = list(HtmlSemanticReader().convert(_doc(html)))
    assert sections[0].anchor == "idx:0"
    assert sections[1].anchor == "idx:1"


def test_semantic_no_blocks_falls_back_to_body():
    html = "<html><body><p>just a paragraph</p></body></html>"
    sections = list(HtmlSemanticReader().convert(_doc(html)))
    assert len(sections) == 1
    assert sections[0].anchor is None
    assert "just a paragraph" in sections[0].content


# ----------------------------- HtmlReadabilityReader ---------------------------


# Skip if trafilatura not installed — это опциональная зависимость.
trafilatura = pytest.importorskip("trafilatura")


def test_readability_reader_id():
    assert HtmlReadabilityReader().reader_id() == ReaderId("ext.html.readability")


def test_readability_extracts_main_content_skipping_boilerplate():
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
    sections = list(HtmlReadabilityReader().convert(_doc(html)))
    # trafilatura должен отбросить nav и footer, оставить article-текст.
    assert len(sections) == 1
    s = sections[0]
    assert "main story" in s.content
    assert "Menu" not in s.content
    assert "(c) 2026" not in s.content
    assert s.anchor is None


def test_readability_empty_payload_yields_nothing():
    assert list(HtmlReadabilityReader().convert(_doc(""))) == []
