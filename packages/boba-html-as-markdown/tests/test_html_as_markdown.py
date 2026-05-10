"""HtmlMarkdownifyReader / HtmlExtractedMarkdownifyReader: composite HTML→MD readers."""

from __future__ import annotations

from io import BytesIO

import pytest

# Skip if markdownify not installed (хотя оно required-dep этого пакета,
# в isolated test envs может быть не установлено).
pytest.importorskip("markdownify")

from boba.indexing import (
    Metadata,
    RawDocument,
    ReaderId,
    SourceId,
)
from boba.html_as_markdown import (
    HtmlExtractedMarkdownifyReader,
    HtmlMarkdownifyReader,
)
from boba.markdown import MarkdownKeys


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


# ------------------------------ HtmlMarkdownifyReader --------------------------


def test_markdownify_reader_id():
    assert HtmlMarkdownifyReader().reader_id() == ReaderId("ext.html.markdownify")


def test_markdownify_yields_section_per_heading_with_md_content():
    """HTML с h1/h2 → markdown через markdownify → 2 Section'и; content — markdown."""
    html = (
        "<html><body>"
        "<h1>Intro</h1><p>intro <strong>body</strong></p>"
        "<h2>API</h2><ul><li>one</li><li>two</li></ul>"
        "</body></html>"
    )
    sections = list(HtmlMarkdownifyReader().convert(_doc(html)))
    assert len(sections) == 2

    # Section #1: heading "Intro" + markdown body со звёздочками для <strong>.
    assert sections[0].anchor == "intro"
    assert sections[0].metadata.get(MarkdownKeys.HEADING_LEVEL) == 1
    assert sections[0].metadata.get(MarkdownKeys.HEADING_TEXT) == "Intro"
    assert "Intro" in sections[0].content
    # markdownify конвертирует <strong> в **...**
    assert "**body**" in sections[0].content

    # Section #2: heading "API" + markdown list.
    assert sections[1].anchor == "api"
    assert sections[1].metadata.get(MarkdownKeys.HEADING_LEVEL) == 2
    assert "API" in sections[1].content
    assert "one" in sections[1].content
    assert "two" in sections[1].content


def test_markdownify_metadata_merge_from_raw_document():
    html = "<html><body><h1>A</h1><p>x</p></body></html>"
    upstream = Metadata.from_wire({"source_url": "https://example.com/page"})
    sections = list(HtmlMarkdownifyReader().convert(_doc(html, metadata=upstream)))
    assert (
        sections[0].metadata.to_wire()["source_url"]
        == "https://example.com/page"
    )


def test_markdownify_options_passed_through():
    """markdownify_options прокидывается в markdownify.markdownify(...)."""
    html = "<html><body><h1>T</h1><p><a href='x'>link</a></p></body></html>"
    reader = HtmlMarkdownifyReader(markdownify_options={"strip": ["a"]})
    sections = list(reader.convert(_doc(html)))
    assert "link" in sections[0].content
    assert "[link]" not in sections[0].content


def test_markdownify_empty_payload_yields_nothing():
    assert list(HtmlMarkdownifyReader().convert(_doc(""))) == []


# ------------------------- HtmlExtractedMarkdownifyReader ----------------------


# Skip if trafilatura not installed.
pytest.importorskip("trafilatura")


def test_extracted_markdownify_reader_id():
    assert (
        HtmlExtractedMarkdownifyReader().reader_id()
        == ReaderId("ext.html.extracted_markdownify")
    )


def test_extracted_markdownify_drops_boilerplate_and_yields_markdown_section():
    html = (
        "<html><body>"
        "<nav>Menu | Home | About</nav>"
        "<article>"
        "<h1>News title</h1>"
        "<p>Important <strong>story</strong> with substantial body text "
        "that trafilatura would consider main content for extraction.</p>"
        "<ul><li>fact one is here</li><li>fact two is here</li></ul>"
        "</article>"
        "<footer>(c) 2026 example.com</footer>"
        "</body></html>"
    )
    sections = list(HtmlExtractedMarkdownifyReader().convert(_doc(html)))
    assert len(sections) >= 1

    combined = "\n\n".join(s.content for s in sections)
    assert "Menu" not in combined
    assert "(c) 2026" not in combined
    # NB: trafilatura.extract(output_format="html") стриппит inline-форматирование,
    # так что **bold** не выживает. Block-структура (heading + list) сохраняется.
    assert "fact one" in combined
    assert "fact two" in combined
    assert any(s.anchor == "news-title" for s in sections)


def test_extracted_markdownify_empty_payload_yields_nothing():
    assert list(HtmlExtractedMarkdownifyReader().convert(_doc(""))) == []
