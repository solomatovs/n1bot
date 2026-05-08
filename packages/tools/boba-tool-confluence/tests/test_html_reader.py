"""HtmlReader: heading-aware split + fallback + edge cases."""

from __future__ import annotations

from io import BytesIO

from boba.html_reader import HtmlReader
from boba.processing import IndexingContext, PipelineId, RawDocument, ReaderId


def _ctx() -> IndexingContext:
    return IndexingContext(pipeline_id=PipelineId("t"), collection="c")


def _doc(html: str, *, source_id: str = "fs:/x", metadata=None) -> RawDocument:
    return RawDocument(
        handle=BytesIO(html.encode("utf-8")),
        source_id=source_id,
        content_hint="html",
        metadata=metadata or {},
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
    assert "One" in sections[0].text
    assert "alpha" in sections[0].text
    assert "beta" not in sections[0].text
    assert sections[1].anchor == "idx:2"
    assert "Two" in sections[1].text
    assert "beta gamma" in sections[1].text


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
    assert "Doc Title" in sections[0].text
    assert "just paragraphs" in sections[0].text


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
    assert "real content" in sections[0].text


def test_script_and_style_dropped():
    html = (
        "<html><body><script>alert('x')</script>"
        "<h1>OK</h1><style>h1{}</style><p>body</p></body></html>"
    )
    sections = list(HtmlReader().convert(_doc(html)))
    assert "alert" not in sections[0].text
    assert "h1{}" not in sections[0].text
    assert "OK" in sections[0].text
    assert "body" in sections[0].text


def test_metadata_merge_from_raw_document():
    """upstream metadata (например source_url) пробрасывается в Section."""
    html = "<html><body><h1>A</h1><p>x</p></body></html>"
    sections = list(
        HtmlReader().convert(
            _doc(html, metadata={"source_url": "https://example.com/page"}),
        )
    )
    assert sections[0].metadata["source_url"] == "https://example.com/page"
    assert sections[0].metadata["heading_text"] == "A"


def test_empty_payload_yields_nothing():
    sections = list(HtmlReader().convert(_doc("")))
    assert sections == []
