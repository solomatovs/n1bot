"""ConfluenceReader: SourceItem(content_hint='confluence_html') → Section[].

Heading-aware split: каждая heading-секция (h1..h6) → отдельная Section с
anchor'ом из confluence-bookmark или idx:N. Текст содержит сам heading +
содержимое до следующего heading. Содержимое ac:* / ri:* макросов
исключается.
"""

from __future__ import annotations

from collections.abc import Iterable

from bs4.element import NavigableString, Tag

from boba.ext.confluence_shared import (
    Heading,
    anchor_for,
    collect_headings,
    parse_html,
)
from boba.indexing import (
    IndexingContext,
    Reader,
    ReaderId,
    Section,
    SourceItem,
)

__all__ = ["ConfluenceReader"]

_READER_ID = ReaderId("ext.confluence")
_HEADING_TAG_NAMES = frozenset({"h1", "h2", "h3", "h4", "h5", "h6"})


class ConfluenceReader(Reader):
    """Heading-aware Reader для Confluence-export HTML."""

    def name(self) -> str:
        return "ConfluenceReader"

    def reader_id(self) -> ReaderId:
        return _READER_ID

    def accepts(self, item: SourceItem) -> bool:
        return item.content_hint == "confluence_html"

    def convert(
        self, ctx: IndexingContext, value: SourceItem
    ) -> Iterable[Section]:
        del ctx
        if not value.payload.strip():
            return
        soup = parse_html(value.payload)
        body = soup.body or soup
        # Heading без текста (например навигационная h1 из одних картинок)
        # — не информативен и порождает пустые Section'ы. Skip.
        headings = [h for h in collect_headings(soup) if h.text.strip()]

        title = value.metadata.get("title", "").strip()

        if not headings:
            yield from self._fallback_section(value, body, title)
            return

        for i, h in enumerate(headings):
            next_tag = headings[i + 1].tag if i + 1 < len(headings) else None
            between = _text_between(h.tag, next_tag)
            text = h.text + (("\n\n" + between) if between else "")
            yield Section(
                source_id=value.source_id,
                text=text.strip(),
                anchor=anchor_for(h),
                order=h.index,
                content_hash=value.content_hash,
                metadata={**value.metadata, **_section_metadata(h)},
            )

    def _fallback_section(
        self, value: SourceItem, body: Tag, title: str
    ) -> Iterable[Section]:
        """Body без heading'ов: одна Section c title как корневым заголовком."""
        text = _plain_text_full(body)
        if not text and not title:
            return
        composed = f"{title}\n\n{text}".strip() if title else text
        yield Section(
            source_id=value.source_id,
            text=composed,
            anchor=None,
            order=0,
            content_hash=value.content_hash,
            metadata={
                **value.metadata,
                "format": "confluence_html",
                "heading_text": title,
            },
        )


def _section_metadata(h: Heading) -> dict[str, str]:
    return {
        "format": "confluence_html",
        "heading_level": str(h.level),
        "heading_text": h.text,
    }


def _is_confluence_macro_parent(el: NavigableString) -> bool:
    return any(
        isinstance(p, Tag) and (p.name or "").startswith(("ac:", "ri:"))
        for p in el.parents
    )


def _is_inside_heading(el: NavigableString) -> bool:
    return any(
        isinstance(p, Tag) and p.name in _HEADING_TAG_NAMES
        for p in el.parents
    )


def _text_between(start_tag: Tag, end_tag: Tag | None) -> str:
    """Конкатенация всех NavigableString между start_tag и end_tag в DOM-порядке.

    Пропускает текст внутри ac:*/ri:* макросов и текст самих heading-тегов
    (он уже хранится в Heading.text).
    """
    parts: list[str] = []
    for el in start_tag.next_elements:
        if el is end_tag:
            break
        if not isinstance(el, NavigableString):
            continue
        if _is_confluence_macro_parent(el) or _is_inside_heading(el):
            continue
        parts.append(str(el))
    return " ".join(" ".join(parts).split())


def _plain_text_full(node: Tag) -> str:
    """Текст всего поддерева без ac:/ri: макросов."""
    parts: list[str] = []
    for el in node.descendants:
        if not isinstance(el, NavigableString):
            continue
        if _is_confluence_macro_parent(el):
            continue
        parts.append(str(el))
    return " ".join(" ".join(parts).split())
