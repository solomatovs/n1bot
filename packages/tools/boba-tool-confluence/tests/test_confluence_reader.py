"""ConfluenceReader: heading-aware split на RawDocument-handle."""

from __future__ import annotations

from io import BytesIO

from boba.ext.confluence_tools.reader import ConfluenceReader
from boba.processing import IndexingContext, PipelineId, RawDocument, ReaderId


def _ctx() -> IndexingContext:
    return IndexingContext(pipeline_id=PipelineId("t"), collection="c")


def _doc(
    html: str,
    *,
    title: str = "",
    source_id: str = "https://confl.test/pages/viewpage.action?pageId=1",
) -> RawDocument:
    metadata = {"title": title} if title else {}
    return RawDocument(
        handle=BytesIO(html.encode("utf-8")),
        source_id=source_id,
        content_hint="confluence_html",
        metadata=metadata,
    )


def test_reader_id():
    assert ConfluenceReader().reader_id() == ReaderId("ext.confluence")


def test_no_headings_yields_single_section():
    html = "<html><body><p>hello world</p></body></html>"
    sections = list(ConfluenceReader().convert(_ctx(), _doc(html)))
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
    sections = list(ConfluenceReader().convert(_ctx(), _doc(html)))
    assert len(sections) == 2
    assert sections[0].anchor == "idx:1"
    assert "One" in sections[0].text
    assert "alpha" in sections[0].text
    assert "beta" not in sections[0].text
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
    sections = list(ConfluenceReader().convert(_ctx(), _doc(html)))
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
    sections = list(ConfluenceReader().convert(_ctx(), _doc(html)))
    assert len(sections) == 1
    assert sections[0].anchor == "scroll-bookmark-7"
    assert "Заголовок" in sections[0].text
    assert "тело" in sections[0].text


def test_empty_payload_yields_nothing():
    assert list(ConfluenceReader().convert(_ctx(), _doc(""))) == []


def test_section_metadata_carries_heading_level_and_text():
    html = "<html><body><h3>H3</h3><p>x</p></body></html>"
    sections = list(ConfluenceReader().convert(_ctx(), _doc(html)))
    assert sections[0].metadata["heading_level"] == "3"
    assert sections[0].metadata["heading_text"] == "H3"
    assert sections[0].metadata["format"] == "confluence_html"


def test_empty_headings_skipped():
    """Навигационные h1 (только картинки) — пропускаются."""
    html = """
    <html><body>
      <p>body content</p>
      <h1><img src="prev.png"/></h1>
      <h1><img src="home.png"/></h1>
      <h1><img src="next.png"/></h1>
    </body></html>
    """
    sections = list(ConfluenceReader().convert(_ctx(), _doc(html, title="My Page")))
    assert len(sections) == 1
    assert sections[0].anchor is None
    assert "My Page" in sections[0].text
    assert "body content" in sections[0].text


def test_fallback_uses_title_when_no_real_headings():
    html = "<html><body><p>just paragraphs here</p></body></html>"
    sections = list(ConfluenceReader().convert(_ctx(), _doc(html, title="Page Title")))
    assert len(sections) == 1
    assert sections[0].text.startswith("Page Title")
    assert "just paragraphs here" in sections[0].text
    assert sections[0].metadata["heading_text"] == "Page Title"


def test_fallback_without_title_still_yields_section_if_body_has_text():
    html = "<html><body><p>orphan body</p></body></html>"
    sections = list(ConfluenceReader().convert(_ctx(), _doc(html)))
    assert len(sections) == 1
    assert "orphan body" in sections[0].text


def test_fallback_yields_nothing_for_empty_body_and_no_title():
    doc = _doc("<html><body></body></html>")
    assert list(ConfluenceReader().convert(_ctx(), doc)) == []


def test_metadata_merge_from_raw_document():
    """upstream metadata (например confluence_page_id, source_url) пробрасывается."""
    html = "<html><body><h1>T</h1><p>x</p></body></html>"
    doc = RawDocument(
        handle=BytesIO(html.encode("utf-8")),
        source_id="https://confl.test/pages/viewpage.action?pageId=777",
        content_hint="confluence_html",
        metadata={
            "title": "Page",
            "confluence_page_id": "777",
            "source_url": "https://x/pages/viewpage.action?pageId=777",
        },
    )
    sections = list(ConfluenceReader().convert(_ctx(), doc))
    assert sections[0].metadata["confluence_page_id"] == "777"
    assert sections[0].metadata["source_url"].startswith("https://x/")
    assert sections[0].metadata["heading_text"] == "T"
