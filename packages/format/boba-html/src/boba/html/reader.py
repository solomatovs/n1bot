"""HTML reader'ы: HTML → типизированные `Section`-ы.

Несколько вариантов резки HTML под разные типы документов:

- `HtmlHeadingReader`      — режет по `<h1>..<h6>`. Каждый heading → отдельная
  `HeadingSection`, идущий за ним body → `ParagraphSection`. `StructuralChunker`
  сделает heading-prefix-merge при чанковании.
- `HtmlPlainReader`        — весь body как один `ParagraphSection` (+ `<title>`).
- `HtmlSemanticReader`     — каждый top-level `<article>`/`<section>` → один
  `ParagraphSection` с anchor'ом из html-id.
- `HtmlReadabilityReader`  — content-extraction через `trafilatura`
  (boilerplate-removal); один `ParagraphSection` с очищенным текстом.

Все HTML-reader'ы:
- удаляют `<script>`/`<style>` из выдачи;
- кладут `<title>` (если есть) в `ReaderKeys.PAGE_TITLE`;
- пробрасывают `RawDocument.source_id` и мержат `RawDocument.metadata` в Section.

**Замечание про `location`**: BeautifulSoup не даёт точные char-offset'ы тегов
в исходном payload, поэтому HTML-reader'ы пока проставляют
`location=ChunkLocation(0, len(content))` (не invariant-correct, но работает
с `StructuralChunker` через локальные индексы внутри `section.content`).
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import ClassVar

from boba.indexing import (
    ChunkLocation,
    HeadingSection,
    ParagraphSection,
    RawDocument,
    Reader,
    ReaderId,
    ReaderKeys,
    Section,
)
from boba.html.parser import (
    anchor_for,
    collect_headings,
    parse_html,
    plain_text,
    text_between,
)

try:
    import trafilatura as _trafilatura
except ImportError:  # optional dependency — see HtmlReadabilityReader
    _trafilatura = None  # type: ignore[assignment]

__all__ = [
    "HtmlHeadingReader",
    "HtmlPlainReader",
    "HtmlReadabilityReader",
    "HtmlSemanticReader",
]


class _HtmlBase:
    """Общие утилиты для HTML reader'ов: noise-фильтр, title."""

    DOC_TYPE: ClassVar[str] = "html"
    NOISE_TAGS: ClassVar[tuple[str, ...]] = ("script", "style")

    @classmethod
    def _strip_noise(cls, soup) -> None:
        for tag in soup(list(cls.NOISE_TAGS)):
            tag.decompose()

    @staticmethod
    def _extract_title(soup) -> str:
        title_tag = soup.find("title")
        return title_tag.get_text(strip=True) if title_tag else ""

    @staticmethod
    def _location_for(content: str) -> ChunkLocation:
        """Default location: span of content. Не точный offset в исходнике —
        BeautifulSoup не даёт char-offset'ов; для accurate location HTML-reader'ам
        нужно lxml sourceline-tracking (TODO).
        """
        return ChunkLocation(start=0, end=len(content))


class HtmlHeadingReader(_HtmlBase, Reader[str]):
    """Heading-based HTML reader: режет по `<h1>..<h6>` на типизированные секции.

    Для каждого heading'а эмитятся:
    - `HeadingSection` с `level` / `text` / anchor (html-id или `idx:N`);
    - `ParagraphSection` с body-текстом до следующего heading'а
      (только если body не пустой).

    `StructuralChunker` затем делает heading-prefix-merge: heading
    присоединяется как префикс к следующей `ParagraphSection` в `format_content`.

    Если в документе нет heading'ов — fallback одного `ParagraphSection`
    со всем body + `<title>`.
    """

    READER_ID: ClassVar[ReaderId] = ReaderId("ext.html.heading")

    def name(self) -> str:
        return "HtmlHeadingReader"

    def reader_id(self) -> ReaderId:
        return self.READER_ID

    def convert(self, value: RawDocument) -> Iterable[Section[str]]:
        payload = value.handle.read()
        if not payload.strip():
            return
        soup = parse_html(payload)
        self._strip_noise(soup)

        title = self._extract_title(soup)
        body = soup.body or soup
        headings = [h for h in collect_headings(soup) if h.text.strip()]

        if not headings:
            yield from self._fallback(value, body, title)
            return

        base_meta = self._base_meta(value, title)
        order = 0
        for i, h in enumerate(headings):
            heading_text = h.text
            heading_md = "#" * h.level + " " + heading_text  # canonical text marker
            yield HeadingSection(
                source_id=value.source_id,
                content=heading_md,
                location=self._location_for(heading_md),
                anchor=anchor_for(h),
                order=order,
                metadata=base_meta,
                level=h.level,
                text=heading_text,
            )
            order += 1
            next_tag = headings[i + 1].tag if i + 1 < len(headings) else None
            between = text_between(h.tag, next_tag).strip()
            if between:
                yield ParagraphSection(
                    source_id=value.source_id,
                    content=between,
                    location=self._location_for(between),
                    anchor=None,
                    order=order,
                    metadata=base_meta,
                )
                order += 1

    def _fallback(
        self, value: RawDocument, body, title: str
    ) -> Iterable[Section[str]]:
        text = plain_text(body)
        if not text and not title:
            return
        composed = f"{title}\n\n{text}".strip() if title else text
        yield ParagraphSection(
            source_id=value.source_id,
            content=composed,
            location=self._location_for(composed),
            anchor=None,
            order=0,
            metadata=self._base_meta(value, title),
        )

    def _base_meta(self, value: RawDocument, title: str):
        meta = value.metadata.set(ReaderKeys.DOC_TYPE, self.DOC_TYPE)
        if title:
            meta = meta.set(ReaderKeys.PAGE_TITLE, title)
        return meta


class HtmlPlainReader(_HtmlBase, Reader[str]):
    """Плоский HTML reader: один `ParagraphSection` на весь body + `<title>`.

    Не пытается выделить main content (для этого `HtmlReadabilityReader`),
    не режет по heading'ам (для этого `HtmlHeadingReader`).
    """

    READER_ID: ClassVar[ReaderId] = ReaderId("ext.html.plain")

    def name(self) -> str:
        return "HtmlPlainReader"

    def reader_id(self) -> ReaderId:
        return self.READER_ID

    def convert(self, value: RawDocument) -> Iterable[Section[str]]:
        payload = value.handle.read()
        if not payload.strip():
            return
        soup = parse_html(payload)
        self._strip_noise(soup)

        title = self._extract_title(soup)
        body = soup.body or soup
        text = plain_text(body)
        if not text and not title:
            return

        composed = f"{title}\n\n{text}".strip() if title else text
        meta = value.metadata.set(ReaderKeys.DOC_TYPE, self.DOC_TYPE)
        if title:
            meta = meta.set(ReaderKeys.PAGE_TITLE, title)
        yield ParagraphSection(
            source_id=value.source_id,
            content=composed,
            location=self._location_for(composed),
            anchor=None,
            order=0,
            metadata=meta,
        )


class HtmlSemanticReader(_HtmlBase, Reader[str]):
    """HTML5-semantic reader: режет по top-level `<article>` и `<section>`.

    Каждый top-level семантический блок → один `ParagraphSection` с anchor'ом
    из html-id (или fallback `idx:N`). Без top-level блоков — fallback на
    `<main>` или body как один `ParagraphSection`.

    Top-level: блок без предка `<article>`/`<section>` — это избегает дублей
    при вложенных семантических блоках.
    """

    READER_ID: ClassVar[ReaderId] = ReaderId("ext.html.semantic")
    SEMANTIC_TAGS: ClassVar[tuple[str, ...]] = ("article", "section")

    def name(self) -> str:
        return "HtmlSemanticReader"

    def reader_id(self) -> ReaderId:
        return self.READER_ID

    def convert(self, value: RawDocument) -> Iterable[Section[str]]:
        payload = value.handle.read()
        if not payload.strip():
            return
        soup = parse_html(payload)
        self._strip_noise(soup)

        title = self._extract_title(soup)
        blocks = self._collect_top_level(soup)
        meta_base = value.metadata.set(ReaderKeys.DOC_TYPE, self.DOC_TYPE)
        if title:
            meta_base = meta_base.set(ReaderKeys.PAGE_TITLE, title)

        if not blocks:
            body = soup.find("main") or soup.body or soup
            text = plain_text(body)
            if not text and not title:
                return
            composed = f"{title}\n\n{text}".strip() if title else text
            yield ParagraphSection(
                source_id=value.source_id,
                content=composed,
                location=self._location_for(composed),
                anchor=None,
                order=0,
                metadata=meta_base,
            )
            return

        for i, block in enumerate(blocks):
            text = plain_text(block).strip()
            if not text:
                continue
            anchor = block.get("id") or f"idx:{i}"
            yield ParagraphSection(
                source_id=value.source_id,
                content=text,
                location=self._location_for(text),
                anchor=anchor,
                order=i,
                metadata=meta_base,
            )

    @classmethod
    def _collect_top_level(cls, soup) -> list:
        """Собирает `<article>`/`<section>`, не имеющие предка того же типа."""
        all_blocks = soup.find_all(list(cls.SEMANTIC_TAGS))
        return [
            b for b in all_blocks if not b.find_parent(list(cls.SEMANTIC_TAGS))
        ]


class HtmlReadabilityReader(_HtmlBase, Reader[str]):
    """Content-extraction reader: `trafilatura` отбрасывает boilerplate,
    результат → один `ParagraphSection`.

    **Зависимость**: требует `trafilatura` (опциональная).
    Установка: `pip install boba-html[readability]`.
    """

    READER_ID: ClassVar[ReaderId] = ReaderId("ext.html.readability")

    def name(self) -> str:
        return "HtmlReadabilityReader"

    def reader_id(self) -> ReaderId:
        return self.READER_ID

    def convert(self, value: RawDocument) -> Iterable[Section[str]]:
        if _trafilatura is None:
            raise ImportError(
                "HtmlReadabilityReader requires `trafilatura`. "
                "Install: `pip install trafilatura` or "
                "`pip install boba-html[readability]`."
            )

        payload = value.handle.read()
        if not payload.strip():
            return

        text = _trafilatura.extract(
            payload, output_format="txt", include_comments=False
        )
        if not text:
            return

        soup = parse_html(payload)
        self._strip_noise(soup)
        title = self._extract_title(soup)

        meta = value.metadata.set(ReaderKeys.DOC_TYPE, self.DOC_TYPE)
        if title:
            meta = meta.set(ReaderKeys.PAGE_TITLE, title)

        cleaned = text.strip()
        yield ParagraphSection(
            source_id=value.source_id,
            content=cleaned,
            location=self._location_for(cleaned),
            anchor=None,
            order=0,
            metadata=meta,
        )
