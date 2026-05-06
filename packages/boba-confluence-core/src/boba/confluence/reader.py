"""ConfluenceReader: RawDocument → Section[] для Confluence-export HTML.

Heading-aware split: каждая heading-секция (h1..h6) → отдельная Section с
anchor'ом из confluence-bookmark или idx:N. Текст содержит сам heading +
содержимое до следующего heading. Содержимое ac:*/ri:* макросов
исключается.

Pipeline-плагин подключает этот Reader явно для request-source'ов, которые
выдают confluence-export'ы (например `ConfluenceSpaceRequestSource`).
"""

from __future__ import annotations

from collections.abc import Iterable

from bs4.element import Tag

from boba.confluence.parse import (
    Heading,
    anchor_for,
    collect_headings,
    parse_html,
    plain_text,
    text_between,
)
from boba.processing import (
    IndexingContext,
    RawDocument,
    Reader,
    ReaderId,
    Section,
)

__all__ = ["ConfluenceReader"]

_READER_ID = ReaderId("ext.confluence")
_FORMAT = "confluence_html"


class ConfluenceReader(Reader):
    """Heading-aware Reader для Confluence-export HTML."""

    def name(self) -> str:
        return "ConfluenceReader"

    def reader_id(self) -> ReaderId:
        return _READER_ID

    def convert(
        self, ctx: IndexingContext, value: RawDocument
    ) -> Iterable[Section]:
        del ctx
        payload = value.handle.read()
        if not payload.strip():
            return
        soup = parse_html(payload)
        body = soup.body or soup
        # Heading без текста (навигационная h1 из одних картинок) — skip.
        headings = [h for h in collect_headings(soup) if h.text.strip()]

        title = str(value.metadata.get("title", "")).strip()

        if not headings:
            yield from self._fallback_section(value, body, title)
            return

        for i, h in enumerate(headings):
            next_tag = headings[i + 1].tag if i + 1 < len(headings) else None
            between = text_between(h.tag, next_tag)
            text = h.text + (("\n\n" + between) if between else "")
            yield Section(
                source_id=value.source_id,
                text=text.strip(),
                anchor=anchor_for(h),
                order=h.index,
                metadata={**value.metadata, **_section_metadata(h)},
            )

    def _fallback_section(
        self, value: RawDocument, body: Tag, title: str
    ) -> Iterable[Section]:
        """Body без heading'ов: одна Section c title как корневым заголовком."""
        text = plain_text(body)
        if not text and not title:
            return
        composed = f"{title}\n\n{text}".strip() if title else text
        yield Section(
            source_id=value.source_id,
            text=composed,
            anchor=None,
            order=0,
            metadata={
                **value.metadata,
                "format": _FORMAT,
                "heading_text": title,
            },
        )


def _section_metadata(h: Heading) -> dict[str, str]:
    return {
        "format": _FORMAT,
        "heading_level": str(h.level),
        "heading_text": h.text,
    }
