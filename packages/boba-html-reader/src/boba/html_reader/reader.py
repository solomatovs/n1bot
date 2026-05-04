"""HtmlReader: HTML → heading-aware Section'ы.

Структурно как ConfluenceReader, но без Confluence-специфики (`ac:*`).
Для каждого `<h1>..<h6>` создаёт свою Section с anchor'ом из html `id`
(или fallback `idx:N`). Текст Section'и — текст самого heading'а +
всё содержимое до следующего heading'а.

Если в документе нет heading'ов — fallback одной Section со всем
текстом body + title (если есть `<title>`).
"""

from __future__ import annotations

from collections.abc import Iterable

from boba.html_parser import (
    anchor_for,
    collect_headings,
    parse_html,
    plain_text,
    text_between,
)
from boba.indexing import (
    IndexingContext,
    RawDocument,
    Reader,
    ReaderId,
    Section,
)

__all__ = ["HtmlReader"]


_NOISE_TAGS = ("script", "style")
_FORMAT = "html"


class HtmlReader(Reader):
    """HTML → heading-aware Section'ы. Drops <script>/<style>."""

    def name(self) -> str:
        return "HtmlReader"

    def reader_id(self) -> ReaderId:
        return ReaderId("ext.html")

    def convert(
        self, ctx: IndexingContext, value: RawDocument
    ) -> Iterable[Section]:
        del ctx
        payload = value.handle.read()
        if not payload.strip():
            return
        soup = parse_html(payload)
        for tag in soup(list(_NOISE_TAGS)):
            tag.decompose()

        title_tag = soup.find("title")
        title = title_tag.get_text(strip=True) if title_tag else ""
        body = soup.body or soup
        # Heading'и без текста — неинформативные, пропускаем.
        headings = [h for h in collect_headings(soup) if h.text.strip()]

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
                metadata={
                    **value.metadata,
                    "format": _FORMAT,
                    "heading_level": str(h.level),
                    "heading_text": h.text,
                    **({"title": title} if title else {}),
                },
            )

    def _fallback_section(
        self, value: RawDocument, body, title: str
    ) -> Iterable[Section]:
        """HTML без heading'ов: одна Section с title + полным body-текстом."""
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
                **({"title": title, "heading_text": title} if title else {}),
            },
        )
