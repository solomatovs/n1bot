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


def test_markdownify_yields_typed_sections_with_md_content():
    """HTML → markdown via markdownify → типизированные Section'ы с markdown-content."""
    from boba.indexing import HeadingSection, ListSection, ParagraphSection

    html = (
        "<html><body>"
        "<h1>Intro</h1><p>intro <strong>body</strong></p>"
        "<h2>API</h2><ul><li>one</li><li>two</li></ul>"
        "</body></html>"
    )
    sections = list(HtmlMarkdownifyReader().convert(_doc(html)))

    headings = [s for s in sections if isinstance(s, HeadingSection)]
    paragraphs = [s for s in sections if isinstance(s, ParagraphSection)]
    lists = [s for s in sections if isinstance(s, ListSection)]

    # 2 heading'а: Intro (h1) + API (h2).
    assert {(h.level, h.text) for h in headings} == {(1, "Intro"), (2, "API")}
    assert {h.anchor for h in headings} == {"intro", "api"}

    # markdownify конвертирует <strong> в **...** — есть в paragraph.
    assert any("**body**" in p.content for p in paragraphs)
    # <ul><li> → ListSection с распарсенными items.
    assert any({"one", "two"}.issubset(set(lst.items)) for lst in lists)


def test_markdownify_metadata_merge_from_raw_document():
    html = "<html><body><h1>A</h1><p>x</p></body></html>"
    upstream = Metadata.from_wire({"source_url": "https://example.com/page"})
    sections = list(HtmlMarkdownifyReader().convert(_doc(html, metadata=upstream)))
    for s in sections:
        assert (
            s.metadata.to_wire()["source_url"]
            == "https://example.com/page"
        )


def test_markdownify_options_passed_through():
    """markdownify_options прокидывается в markdownify.markdownify(...)."""
    from boba.indexing import ParagraphSection

    html = "<html><body><h1>T</h1><p><a href='x'>link</a></p></body></html>"
    reader = HtmlMarkdownifyReader(markdownify_options={"strip": ["a"]})
    sections = list(reader.convert(_doc(html)))
    paragraphs = [s for s in sections if isinstance(s, ParagraphSection)]
    assert paragraphs, "expected at least one ParagraphSection"
    combined = "\n".join(p.content for p in paragraphs)
    # `<a>` должен быть стрипнут — текст ссылки остаётся, markdown-link-синтаксиса нет.
    assert "link" in combined
    assert "[link]" not in combined


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
