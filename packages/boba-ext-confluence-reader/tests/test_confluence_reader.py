"""ConfluenceReader: heading-aware split по фикстурам Confluence-export."""

from __future__ import annotations

from boba.ext.confluence_reader.reader import ConfluenceReader
from boba.indexing import IndexingContext, PipelineId, ReaderId, SourceItem


def _ctx() -> IndexingContext:
    return IndexingContext(pipeline_id=PipelineId("t"), collection="c")


def _item(html: str, *, title: str = "") -> SourceItem:
    return SourceItem(
        source_id="confluence://x/page/1",
        content_hint="confluence_html",
        payload=html.encode("utf-8"),
        content_hash="1",
        metadata={"title": title} if title else {},
    )


def test_reader_id():
    assert ConfluenceReader().reader_id() == ReaderId("ext.confluence")


def test_accepts_only_confluence_html():
    r = ConfluenceReader()
    assert r.accepts(_item("<p>x</p>"))
    item = SourceItem(source_id="x", content_hint="html", payload=b"x")
    assert not r.accepts(item)


def test_no_headings_yields_single_section():
    html = "<html><body><p>hello world</p></body></html>"
    sections = list(ConfluenceReader().convert(_ctx(), _item(html)))
    assert len(sections) == 1
    assert sections[0].anchor is None
    assert "hello world" in sections[0].text


def test_headings_split_per_heading():
    html = """
    <html><body>
      <h1>One</h1><p>alpha</p>
      <h2>Two</h2><p>beta gamma</p>
    </body></html>
    """
    sections = list(ConfluenceReader().convert(_ctx(), _item(html)))
    assert len(sections) == 2
    assert sections[0].anchor == "idx:1"
    assert "One" in sections[0].text
    assert "alpha" in sections[0].text
    assert "beta" not in sections[0].text  # не утёк в первую
    assert sections[1].anchor == "idx:2"
    assert "Two" in sections[1].text
    assert "beta gamma" in sections[1].text


def test_macros_stripped_from_section_text():
    html = """
    <html><body>
      <h1>T</h1><p>visible</p>
      <ac:structured-macro><ac:parameter>hidden</ac:parameter></ac:structured-macro>
      <p>also visible</p>
    </body></html>
    """
    sections = list(ConfluenceReader().convert(_ctx(), _item(html)))
    assert len(sections) == 1
    assert "hidden" not in sections[0].text
    assert "visible" in sections[0].text
    assert "also visible" in sections[0].text


def test_anchor_picked_up_from_confluence_bookmark():
    html = """
    <html><body>
      <h2 id="ignored">
        <ac:structured-macro ac:name="anchor">
          <ac:parameter>scroll-bookmark-7</ac:parameter>
        </ac:structured-macro>
        Заголовок
      </h2>
      <p>тело</p>
    </body></html>
    """
    sections = list(ConfluenceReader().convert(_ctx(), _item(html)))
    assert len(sections) == 1
    assert sections[0].anchor == "scroll-bookmark-7"
    assert "Заголовок" in sections[0].text
    assert "тело" in sections[0].text


def test_empty_payload_yields_nothing():
    sections = list(ConfluenceReader().convert(_ctx(), _item("")))
    assert sections == []


def test_section_metadata_carries_heading_level_and_text():
    html = "<html><body><h3>H3</h3><p>x</p></body></html>"
    sections = list(ConfluenceReader().convert(_ctx(), _item(html)))
    assert sections[0].metadata["heading_level"] == "3"
    assert sections[0].metadata["heading_text"] == "H3"
    assert sections[0].metadata["format"] == "confluence_html"


def test_empty_headings_skipped():
    """Heading'и из одних картинок (без текста) пропускаются — иначе chunker
    получает пустые Section'ы и upsert'ит ноль чанков (реальный bug confluence-
    export'а навигационных h1 с прев/next-кнопками)."""
    html = """
    <html><body>
      <p>body content</p>
      <h1><img src="prev.png"/></h1>
      <h1><img src="home.png"/></h1>
      <h1><img src="next.png"/></h1>
    </body></html>
    """
    sections = list(ConfluenceReader().convert(_ctx(), _item(html, title="My Page")))
    assert len(sections) == 1
    assert sections[0].anchor is None
    assert "My Page" in sections[0].text
    assert "body content" in sections[0].text


def test_fallback_uses_title_when_no_real_headings():
    """Body без heading'ов: fallback Section получает title в text + metadata."""
    html = "<html><body><p>just paragraphs here</p></body></html>"
    sections = list(ConfluenceReader().convert(_ctx(), _item(html, title="Page Title")))
    assert len(sections) == 1
    assert sections[0].text.startswith("Page Title")
    assert "just paragraphs here" in sections[0].text
    assert sections[0].metadata["heading_text"] == "Page Title"


def test_fallback_without_title_still_yields_section_if_body_has_text():
    html = "<html><body><p>orphan body</p></body></html>"
    sections = list(ConfluenceReader().convert(_ctx(), _item(html)))
    assert len(sections) == 1
    assert "orphan body" in sections[0].text


def test_fallback_yields_nothing_for_empty_body_and_no_title():
    item = _item("<html><body></body></html>")
    assert list(ConfluenceReader().convert(_ctx(), item)) == []
