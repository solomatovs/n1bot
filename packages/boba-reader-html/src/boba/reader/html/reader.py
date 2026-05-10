"""HtmlReader: HTML → heading-aware Section[str].

Структурно как ConfluenceReader, но без Confluence-специфики (`ac:*`).
Для каждого `<h1>..<h6>` создаёт свою Section с anchor'ом из html `id`
(или fallback `idx:N`). Текст Section'и — текст самого heading'а +
всё содержимое до следующего heading'а.

Если в документе нет heading'ов — fallback одной Section со всем
текстом body + title (если есть `<title>`).
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import ClassVar

from boba.indexing import (
    RawDocument,
    Reader,
    ReaderId,
    ReaderKeys,
    Section,
)
from boba.reader.html.keys import HtmlKeys
from boba.reader.html.parser import (
    anchor_for,
    collect_headings,
    parse_html,
    plain_text,
    text_between,
)

__all__ = ["HtmlReader"]


_NOISE_TAGS = ("script", "style")


class HtmlReader(Reader[str]):
    """Reader[str] для HTML с h1..h6. Drops <script>/<style>."""

    DOC_TYPE: ClassVar[str] = "html"
    READER_ID: ClassVar[ReaderId] = ReaderId("ext.html")

    def name(self) -> str:
        return "HtmlReader"

    def reader_id(self) -> ReaderId:
        return self.READER_ID

    def convert(self, value: RawDocument) -> Iterable[Section[str]]:
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
            meta = (
                value.metadata
                .set(ReaderKeys.DOC_TYPE, self.DOC_TYPE)
                .set(HtmlKeys.HEADING_LEVEL, h.level)
                .set(HtmlKeys.HEADING_TEXT, h.text)
            )
            if title:
                meta = meta.set(ReaderKeys.PAGE_TITLE, title)
            yield Section(
                source_id=value.source_id,
                content=text.strip(),
                anchor=anchor_for(h),
                order=h.index,
                metadata=meta,
            )

    def _fallback_section(
        self, value: RawDocument, body, title: str
    ) -> Iterable[Section[str]]:
        """HTML без heading'ов: одна Section с title + полным body-текстом."""
        text = plain_text(body)
        if not text and not title:
            return
        composed = f"{title}\n\n{text}".strip() if title else text
        meta = value.metadata.set(ReaderKeys.DOC_TYPE, self.DOC_TYPE)
        if title:
            meta = meta.set(ReaderKeys.PAGE_TITLE, title)
        yield Section(
            source_id=value.source_id,
            content=composed,
            anchor=None,
            order=0,
            metadata=meta,
        )
