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

from boba.html import HtmlKeys
from boba.indexing import (
    RawDocument,
    Reader,
    ReaderId,
    ReaderKeys,
    Section,
    SectionKeys,
)
from boba.tool.kb.confluence.parse import (
    Heading,
    anchor_for,
    collect_headings,
    parse_html,
    plain_text,
    text_between,
)

__all__ = ["ConfluenceReader"]


class ConfluenceReader(Reader[str]):
    """Heading-aware Reader для Confluence-export HTML."""

    DOC_TYPE: ClassVar[str] = "confluence_html"
    READER_ID: ClassVar[ReaderId] = ReaderId("ext.confluence")
    BREADCRUMB_SEPARATOR: ClassVar[str] = " › "
    TITLE_LEVEL: ClassVar[int] = 0

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

        # Стек breadcrumbs: title → корень (level=0), затем h1..h6 по document order.
        stack: list[tuple[int, str]] = []
        if title:
            stack.append((self.TITLE_LEVEL, title))

        for i, h in enumerate(headings):
            self._push_heading(stack, h.level, h.text)
            path = self._render_path(stack)
            next_tag = headings[i + 1].tag if i + 1 < len(headings) else None
            between = text_between(h.tag, next_tag)
            text = h.text + (("\n\n" + between) if between else "")
            yield Section(
                source_id=value.source_id,
                content=text.strip(),
                order=h.index,
                metadata=self._section_meta(value, h, path),
            )

    def _section_meta(self, value: RawDocument, h: Heading, path: str):
        meta = (
            value.metadata
            .set(ReaderKeys.DOC_TYPE, self.DOC_TYPE)
            .set(HtmlKeys.HEADING_LEVEL, h.level)
            .set(HtmlKeys.HEADING_TEXT, h.text)
            .set(SectionKeys.HEADING_PATH, path)
        )
        anchor = anchor_for(h)
        if anchor:
            meta = meta.set(SectionKeys.ANCHOR, anchor)
        return meta

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
            meta = meta.set(SectionKeys.HEADING_PATH, title)
        yield Section(
            source_id=value.source_id,
            content=composed,
            order=0,
            metadata=meta,
        )

    @staticmethod
    def _push_heading(stack: list[tuple[int, str]], level: int, text: str) -> None:
        """Сбросить со стека всё, что глубже или равно текущему level, и положить новый."""
        while stack and stack[-1][0] >= level:
            stack.pop()
        stack.append((level, text))

    @classmethod
    def _render_path(cls, stack: list[tuple[int, str]]) -> str:
        return cls.BREADCRUMB_SEPARATOR.join(text for _, text in stack)
