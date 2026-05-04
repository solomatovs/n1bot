"""HtmlReader: heading-aware split + fallback + edge cases."""

from __future__ import annotations

from boba.ext.html_reader.reader import HtmlReader
from boba.indexing import IndexingContext, PipelineId, ReaderId, SourceItem


def _ctx() -> IndexingContext:
    return IndexingContext(pipeline_id=PipelineId("t"), collection="c")


def _item(html: str, *, title: str = "") -> SourceItem:
    return SourceItem(
        source_id="fs:/x",
        content_hint="html",
        payload=html.encode("utf-8"),
        content_hash="v1",
        metadata={"title": title} if title else {},
    )


def test_reader_id():
    assert HtmlReader().reader_id() == ReaderId("ext.html")


def test_accepts_html_htm_xhtml():
    r = HtmlReader()
    for hint in ("html", "htm", "xhtml"):
        assert r.accepts(_item("<p>x</p>"))
        item = SourceItem(source_id="x", content_hint=hint, payload=b"x")
        assert r.accepts(item)


def test_rejects_other_hints():
    r = HtmlReader()
    item = SourceItem(source_id="x", content_hint="md", payload=b"x")
    assert not r.accepts(item)


def test_headings_yield_separate_sections():
    html = """
    <html><body>
      <h1>One</h1><p>alpha</p>
      <h2>Two</h2><p>beta gamma</p>
    </body></html>
    """
    sections = list(HtmlReader().convert(_ctx(), _item(html)))
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
    sections = list(HtmlReader().convert(_ctx(), _item(html)))
    assert sections[0].anchor == "intro"


def test_no_headings_falls_back_to_single_section_with_title():
    html = (
        "<html><head><title>Doc Title</title></head>"
        "<body><p>just paragraphs</p></body></html>"
    )
    sections = list(HtmlReader().convert(_ctx(), _item(html)))
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
    sections = list(HtmlReader().convert(_ctx(), _item(html, title="My Page")))
    assert len(sections) == 1
    assert sections[0].anchor is None
    assert "real content" in sections[0].text


def test_script_and_style_dropped():
    html = (
        "<html><body><script>alert('x')</script>"
        "<h1>OK</h1><style>h1{}</style><p>body</p></body></html>"
    )
    sections = list(HtmlReader().convert(_ctx(), _item(html)))
    assert "alert" not in sections[0].text
    assert "h1{}" not in sections[0].text
    assert "OK" in sections[0].text
    assert "body" in sections[0].text


def test_content_hash_propagated():
    html = "<html><body><h1>A</h1><p>x</p><h1>B</h1></body></html>"
    item = SourceItem(
        source_id="fs:/x", content_hint="html",
        payload=html.encode("utf-8"), content_hash="v42",
    )
    sections = list(HtmlReader().convert(_ctx(), item))
    assert all(s.content_hash == "v42" for s in sections)


def test_metadata_preserves_source_url_from_item():
    """source_url из item.metadata должен дойти до Section.metadata."""
    html = "<html><body><h1>A</h1><p>x</p></body></html>"
    item = SourceItem(
        source_id="fs:/x", content_hint="html",
        payload=html.encode("utf-8"),
        metadata={"source_url": "https://example.com/page"},
    )
    sections = list(HtmlReader().convert(_ctx(), item))
    assert sections[0].metadata["source_url"] == "https://example.com/page"
    assert sections[0].metadata["heading_text"] == "A"


def test_empty_payload_yields_nothing():
    sections = list(HtmlReader().convert(_ctx(), _item("")))
    assert sections == []
