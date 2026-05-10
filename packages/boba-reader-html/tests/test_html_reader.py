"""HtmlReader: heading-aware split + fallback + edge cases."""

from __future__ import annotations

from io import BytesIO

from boba.indexing import (
    Metadata,
    RawDocument,
    ReaderId,
    SourceId,
)
from boba.reader.html import HtmlKeys, HtmlReader


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


def test_reader_id():
    assert HtmlReader().reader_id() == ReaderId("ext.html")


def test_headings_yield_separate_sections():
    html = """
    <html><body>
      <h1>One</h1><p>alpha</p>
      <h2>Two</h2><p>beta gamma</p>
    </body></html>
    """
    sections = list(HtmlReader().convert(_doc(html)))
    assert len(sections) == 2
    assert sections[0].anchor == "idx:1"
    assert "One" in sections[0].content
    assert "alpha" in sections[0].content
    assert "beta" not in sections[0].content
    assert sections[1].anchor == "idx:2"
    assert "Two" in sections[1].content
    assert "beta gamma" in sections[1].content


def test_anchor_uses_html_id_when_present():
    html = '<html><body><h1 id="intro">Intro</h1><p>x</p></body></html>'
    sections = list(HtmlReader().convert(_doc(html)))
    assert sections[0].anchor == "intro"


def test_no_headings_falls_back_to_single_section_with_title():
    html = (
        "<html><head><title>Doc Title</title></head>"
        "<body><p>just paragraphs</p></body></html>"
    )
    sections = list(HtmlReader().convert(_doc(html)))
    assert len(sections) == 1
    assert sections[0].anchor is None
    assert "Doc Title" in sections[0].content
    assert "just paragraphs" in sections[0].content


def test_empty_headings_skipped():
    """Heading'и из одних картинок (без текста) — пропускаются."""
    html = """
    <html><body>
      <p>real content</p>
      <h1><img src="prev.png"/></h1>
      <h1><img src="home.png"/></h1>
    </body></html>
    """
    sections = list(HtmlReader().convert(_doc(html)))
    assert len(sections) == 1
    assert sections[0].anchor is None
    assert "real content" in sections[0].content


def test_script_and_style_dropped():
    html = (
        "<html><body><script>alert('x')</script>"
        "<h1>OK</h1><style>h1{}</style><p>body</p></body></html>"
    )
    sections = list(HtmlReader().convert(_doc(html)))
    assert "alert" not in sections[0].content
    assert "h1{}" not in sections[0].content
    assert "OK" in sections[0].content
    assert "body" in sections[0].content


def test_metadata_merge_from_raw_document():
    """upstream metadata (например source_url) пробрасывается в Section."""
    html = "<html><body><h1>A</h1><p>x</p></body></html>"
    upstream = Metadata.from_wire({"source_url": "https://example.com/page"})
    sections = list(
        HtmlReader().convert(
            _doc(html, metadata=upstream),
        )
    )
    assert (
        sections[0].metadata.to_wire()["source_url"]
        == "https://example.com/page"
    )
    assert sections[0].metadata.get(HtmlKeys.HEADING_TEXT) == "A"


def test_empty_payload_yields_nothing():
    sections = list(HtmlReader().convert(_doc("")))
    assert sections == []
