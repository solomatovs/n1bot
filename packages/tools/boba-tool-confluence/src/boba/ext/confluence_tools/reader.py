"""ConfluenceReader: RawDocument → Section[str] для Confluence-export HTML.

Heading-aware split: каждая heading-секция (h1..h6) → отдельная Section с
anchor'ом из confluence scroll-bookmark или fallback `idx:N`. Текст
содержит сам heading + содержимое до следующего heading. Содержимое
ac:*/ri:* макросов исключается (служебная разметка экспорта, не контент).

Pipeline-плагин подключает этот Reader явно для request-source'ов, которые
выдают confluence-export'ы (например `ConfluenceSpaceRequestSource`).
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import ClassVar

from bs4.element import Tag

from boba.ext.confluence_tools.parse import (
    Heading,
    anchor_for,
    collect_headings,
    parse_html,
    plain_text,
    text_between,
)
from boba.indexing import (
    RawDocument,
    Reader,
    ReaderId,
    ReaderKeys,
    Section,
)
from boba.html import HtmlKeys

__all__ = ["ConfluenceReader"]


class ConfluenceReader(Reader[str]):
    """Heading-aware Reader для Confluence-export HTML."""

    DOC_TYPE: ClassVar[str] = "confluence_html"
    READER_ID: ClassVar[ReaderId] = ReaderId("ext.confluence")

    def name(self) -> str:
        return "ConfluenceReader"

    def reader_id(self) -> ReaderId:
        return self.READER_ID

    def convert(self, value: RawDocument) -> Iterable[Section[str]]:
        payload = value.handle.read()
        if not payload.strip():
            return
        soup = parse_html(payload)
        body = soup.body or soup
        # Heading без текста (навигационная h1 из одних картинок) — skip.
        headings = [h for h in collect_headings(soup) if h.text.strip()]

        title = value.metadata.get(ReaderKeys.PAGE_TITLE) or ""

        if not headings:
            yield from self._fallback_section(value, body, title)
            return

        for i, h in enumerate(headings):
            next_tag = headings[i + 1].tag if i + 1 < len(headings) else None
            between = text_between(h.tag, next_tag)
            text = h.text + (("\n\n" + between) if between else "")
            yield Section(
                source_id=value.source_id,
                content=text.strip(),
                anchor=anchor_for(h),
                order=h.index,
                metadata=self._section_meta(value, h),
            )

    def _section_meta(self, value: RawDocument, h: Heading):
        return (
            value.metadata
            .set(ReaderKeys.DOC_TYPE, self.DOC_TYPE)
            .set(HtmlKeys.HEADING_LEVEL, h.level)
            .set(HtmlKeys.HEADING_TEXT, h.text)
        )

    def _fallback_section(
        self, value: RawDocument, body: Tag, title: str
    ) -> Iterable[Section[str]]:
        """Body без heading'ов: одна Section с title как корневым заголовком."""
        text = plain_text(body)
        if not text and not title:
            return
        composed = f"{title}\n\n{text}".strip() if title else text
        meta = value.metadata.set(ReaderKeys.DOC_TYPE, self.DOC_TYPE)
        if title:
            meta = meta.set(HtmlKeys.HEADING_TEXT, title)
        yield Section(
            source_id=value.source_id,
            content=composed,
            anchor=None,
            order=0,
            metadata=meta,
        )
