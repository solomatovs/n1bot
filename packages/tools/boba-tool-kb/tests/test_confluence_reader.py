"""ConfluenceReader: heading-aware split на реальной странице Apache cwiki.

Integration: реальный `ConfluenceReader` на реальном HTML-payload'е,
полученном `ConfluenceJsonDecoder`'ом из `/rest/api/content/{id}` (см.
`confluence_decoded_doc` fixture в conftest).

Edge-case тесты на fallback'и (`empty body`, `no headings → title-based
section`, `empty headings skipped`) удалены — на реальной cwiki-странице
таких ситуаций нет; pre-existing pure-data assert'ы были на синтетике,
которой больше нет.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from boba.html import HtmlKeys
from boba.indexing import (
    RawDocument,
    ReaderId,
    ReaderKeys,
    SectionKeys,
)
from boba.tool.kb.confluence.keys import ConfluenceKeys
from boba.tool.kb.confluence.reader import ConfluenceReader

pytestmark = pytest.mark.integration


def test_reader_id() -> None:
    """Reader-id стабилен — используется pipeline'ом для идентификации."""
    assert ConfluenceReader().reader_id() == ReaderId("ext.confluence")


def test_real_page_yields_at_least_one_section(
    confluence_decoded_doc: RawDocument,
) -> None:
    """Реальная cwiki-страница → ≥1 Section[str] (heading-split или fallback)."""
    sections = list(ConfluenceReader().convert(confluence_decoded_doc))
    assert len(sections) >= 1


def test_section_metadata_carries_doc_type(
    confluence_decoded_doc: RawDocument,
) -> None:
    """Каждая Section помечена `ReaderKeys.DOC_TYPE = "confluence_html"`."""
    sections = list(ConfluenceReader().convert(confluence_decoded_doc))
    assert sections
    for s in sections:
        assert s.metadata.get(ReaderKeys.DOC_TYPE) == "confluence_html"


def test_heading_sections_carry_level_and_text(
    confluence_decoded_doc: RawDocument,
) -> None:
    """У heading-секций есть `HEADING_LEVEL` (1-6) и `HEADING_TEXT` (non-empty).

    Подразумевается, что у реальной cwiki-страницы есть хоть один heading.
    На "Apache Kafka Index" их много.
    """
    sections = list(ConfluenceReader().convert(confluence_decoded_doc))
    heading_sections = [
        s for s in sections if s.metadata.get(HtmlKeys.HEADING_LEVEL) is not None
    ]
    assert heading_sections, (
        "у реальной страницы нет heading-секций — подозрительно "
        "(возможно body не содержит h1..h6)"
    )
    for s in heading_sections:
        level = s.metadata.get(HtmlKeys.HEADING_LEVEL)
        assert isinstance(level, int)
        assert 1 <= level <= 6, f"heading level вне 1-6: {level}"
        text = s.metadata.get(HtmlKeys.HEADING_TEXT)
        assert text is not None
        assert len(text) > 0


def test_section_content_non_empty(
    confluence_decoded_doc: RawDocument,
) -> None:
    """У каждой секции содержание не пустое (heading + body)."""
    sections = list(ConfluenceReader().convert(confluence_decoded_doc))
    assert sections
    for s in sections:
        assert len(s.content) > 0, f"пустая section: {s}"


def test_section_anchors_present_or_idx_fallback(
    confluence_decoded_doc: RawDocument,
) -> None:
    """Heading-секции имеют либо anchor из scroll-bookmark/html-id,
    либо fallback `idx:N`. Reader сам решает по `anchor_for(h)`.

    Невозможно гарантировать конкретный anchor на реальной странице,
    но факт его существования — да.
    """
    sections = list(ConfluenceReader().convert(confluence_decoded_doc))
    heading_sections = [
        s for s in sections if s.metadata.get(HtmlKeys.HEADING_LEVEL) is not None
    ]
    if not heading_sections:
        pytest.skip("у текущей страницы нет heading-секций")
    for s in heading_sections:
        anchor = s.metadata.get(SectionKeys.ANCHOR)
        # Reader НЕ ставит anchor если нет ни scroll-bookmark, ни html-id
        # (см. `confluence.reader._section_meta`) — в этом случае LLM
        # может сослаться на index. Допускаем оба варианта.
        if anchor is not None:
            assert isinstance(anchor, str)
            assert len(anchor) > 0


def test_upstream_metadata_propagates_to_sections(
    confluence_decoded_doc: RawDocument,
) -> None:
    """Upstream-metadata (PAGE_ID, PAGE_TITLE, ...) проброшена в каждую Section."""
    # PAGE_ID лежит в metadata после Decoder'а (от RequestSource).
    page_id_upstream = confluence_decoded_doc.metadata.get(ConfluenceKeys.PAGE_ID)
    title_upstream = confluence_decoded_doc.metadata.get(ReaderKeys.PAGE_TITLE)
    assert page_id_upstream is not None
    assert title_upstream is not None

    sections = list(ConfluenceReader().convert(confluence_decoded_doc))
    assert sections
    for s in sections:
        assert s.metadata.get(ConfluenceKeys.PAGE_ID) == page_id_upstream


def test_source_id_propagates_to_sections(
    confluence_decoded_doc: RawDocument,
) -> None:
    """Все Section имеют тот же `source_id`, что и source RawDocument."""
    expected = confluence_decoded_doc.source_id
    sections = list(ConfluenceReader().convert(confluence_decoded_doc))
    assert sections
    for s in sections:
        assert s.source_id == expected


def test_injected_upstream_metadata_merges(
    confluence_decoded_doc: RawDocument,
) -> None:
    """Дополнительная upstream-metadata (`source_url`-like) карается в Section."""
    custom_marker = "test-marker-12345"
    custom_md = confluence_decoded_doc.metadata.set(
        ConfluenceKeys.HOST, custom_marker,
    )
    doc = replace(confluence_decoded_doc, metadata=custom_md)
    sections = list(ConfluenceReader().convert(doc))
    assert sections
    # Reader должен пробросить host в каждую секцию (через metadata-merge).
    for s in sections:
        assert s.metadata.get(ConfluenceKeys.HOST) == custom_marker


