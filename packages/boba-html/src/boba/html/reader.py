"""HTML reader'ы: HTML → Section[str].

Несколько вариантов резки HTML, под разные типы документов:

- `HtmlHeadingReader`      — режет по `<h1>..<h6>`. Дефолт для wiki/доков.
- `HtmlPlainReader`        — весь body как одна Section (+ `<title>`).
- `HtmlSemanticReader`     — режет по top-level `<article>`/`<section>` (HTML5).
- `HtmlReadabilityReader`  — content-extraction через `trafilatura`
  (boilerplate-removal); на выходе plain text.

Все HTML-reader'ы:
- удаляют `<script>`/`<style>` из выдачи;
- кладут `<title>` (если есть) в `ReaderKeys.PAGE_TITLE`;
- пробрасывают `RawDocument.source_id` и мержат `RawDocument.metadata` в Section.
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
from boba.html.keys import HtmlKeys
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
    """Общие утилиты для HTML reader'ов: noise-фильтр, title, базовая metadata."""

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


class HtmlHeadingReader(_HtmlBase, Reader[str]):
    """
    Heading-based HTML reader: режет документ на Section'и по `<h1>..<h6>`.

    **Когда применять**:
    - wiki / документация / Confluence — heading'и реально соответствуют логическим разделам
    - статьи, спецификации, README
    - HTML с осмысленной h-иерархией

    **Когда НЕ применять**:
    - HTML без heading'ов (или с одним h1 на весь документ) → одна гигантская Section
    - heading'и используются стилистически (h3 на каждом параграфе) → слишком мелко
    - произвольный веб с навигацией / футером — heading'и из boilerplate захватываются

    **Схема**:
    ```html
    <html>
      <head><title>Doc</title></head>
      <body>
        <h1 id="intro">Intro</h1>
        <p>intro body</p>

        <h2 id="api">API</h2>
        <p>api body</p>
      </body>
    </html>
    ```
    ```python
    ──reader.convert(raw)──→ 2 Section'и (по одному на heading)
    ```

    **Поведение**:
    - `<script>`/`<style>` декомпозятся (текст не попадает в Section'и).
    - `anchor` берётся из html `id` heading'а; fallback — `"idx:N"` (порядковый).
    - Heading'и с пустым текстом пропускаются как неинформативные.
    - Без heading'ов — одна Section со всем `body` + `<title>` (если есть); `anchor=None`, `order=0`.
    - Если документ пуст — итератор пуст.

    **Пример**:
    ```python
    reader = HtmlHeadingReader()
    raw = RawDocument(
        handle=BytesIO(
            b"<html><head><title>Doc</title></head><body>"
            b"<h1 id='intro'>Intro</h1><p>intro body</p>"
            b"<h2 id='api'>API</h2><p>api body</p>"
            b"</body></html>"
        ),
        source_id=SourceId("doc1"),
    )

    # Документ с 2 heading'ами → 2 Section'и; <title> кладётся в metadata.PAGE_TITLE.
    list(reader.convert(raw)) == [
        Section(
            source_id=SourceId("doc1"),         # pass из RawDocument
            content="Intro\\n\\nintro body",     # новое: heading + текст до следующего heading'а
            anchor="intro",                     # новое: html id="intro" → anchor
            order=1,                            # новое: порядок heading'а в документе
            metadata=(                          # merge: + DOC_TYPE / HEADING_* / PAGE_TITLE
                Metadata.empty()
                .set(ReaderKeys.DOC_TYPE, "html")
                .set(HtmlKeys.HEADING_LEVEL, 1)
                .set(HtmlKeys.HEADING_TEXT, "Intro")
                .set(ReaderKeys.PAGE_TITLE, "Doc")
            ),
        ),
        Section(
            source_id=SourceId("doc1"),
            content="API\\n\\napi body",
            anchor="api",
            order=2,
            metadata=(
                Metadata.empty()
                .set(ReaderKeys.DOC_TYPE, "html")
                .set(HtmlKeys.HEADING_LEVEL, 2)
                .set(HtmlKeys.HEADING_TEXT, "API")
                .set(ReaderKeys.PAGE_TITLE, "Doc")
            ),
        ),
    ]
    ```
    """  # noqa: E501

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


class HtmlPlainReader(_HtmlBase, Reader[str]):
    """
    Плоский HTML reader: одна Section на весь body + `<title>` (без heading-резки).

    **Когда применять**:
    - HTML без структуры или с непредсказуемой структурой
    - лендинги, e-mail-нотификации, simple landing pages
    - safe fallback: дальше пайплайна chunk_size порежет по символам через Splitter

    **Не делает**: не пытается выделить main content (для этого `HtmlReadabilityReader`),
    не режет по семантике (для этого `HtmlSemanticReader`).

    **Пример**:
    ```python
    reader = HtmlPlainReader()
    raw = RawDocument(
        handle=BytesIO(
            b"<html><head><title>Page</title></head>"
            b"<body><p>first paragraph</p><p>second paragraph</p></body></html>"
        ),
        source_id=SourceId("doc1"),
    )

    # 1 Section: title + всё body-текст.
    list(reader.convert(raw)) == [
        Section(
            source_id=SourceId("doc1"),                      # pass из RawDocument
            content="Page\\n\\nfirst paragraph\\nsecond paragraph",  # новое: title + body
            anchor=None,                                     # новое: нет heading-якоря
            order=0,                                         # новое
            metadata=(                                       # merge: + DOC_TYPE / PAGE_TITLE
                Metadata.empty()
                .set(ReaderKeys.DOC_TYPE, "html")
                .set(ReaderKeys.PAGE_TITLE, "Page")
            ),
        ),
    ]
    ```
    """  # noqa: E501

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
        yield Section(
            source_id=value.source_id,
            content=composed,
            anchor=None,
            order=0,
            metadata=meta,
        )


class HtmlSemanticReader(_HtmlBase, Reader[str]):
    """
    HTML5-semantic reader: режет по top-level `<article>` и `<section>`.

    **Когда применять**:
    - современные сайты с HTML5 semantic markup
    - страницы где `<article>`/`<section>` несут структуру лучше, чем h1..h6
    - SPAs где heading'и могут отсутствовать или быть в boilerplate

    **Что считается top-level**: `<article>`/`<section>`, у которых нет родителя
    `<article>`/`<section>` среди предков. Это избегает дублей при вложенных блоках.

    **Anchor**: id тега если есть, иначе `"idx:N"` (порядковый).

    **Без top-level `<article>`/`<section>`** — fallback на `<main>` целиком,
    либо `<body>` целиком (одна Section, как `HtmlPlainReader`).

    **Пример**:
    ```python
    reader = HtmlSemanticReader()
    raw = RawDocument(
        handle=BytesIO(
            b"<html><body>"
            b"<article id='post-1'><h2>Post 1</h2><p>alpha</p></article>"
            b"<article id='post-2'><h2>Post 2</h2><p>beta</p></article>"
            b"</body></html>"
        ),
        source_id=SourceId("doc1"),
    )

    # 2 <article> → 2 Section'и; anchor берётся из id.
    list(reader.convert(raw)) == [
        Section(
            source_id=SourceId("doc1"),                      # pass
            content="Post 1\\nalpha",                        # новое: текст блока
            anchor="post-1",                                 # новое: id="post-1"
            order=0,                                         # новое
            metadata=Metadata.empty().set(ReaderKeys.DOC_TYPE, "html"),  # merge: + DOC_TYPE
        ),
        Section(
            source_id=SourceId("doc1"),
            content="Post 2\\nbeta",
            anchor="post-2",
            order=1,
            metadata=Metadata.empty().set(ReaderKeys.DOC_TYPE, "html"),
        ),
    ]
    ```
    """  # noqa: E501

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

        if not blocks:
            # Нет <article>/<section> — fallback одной Section на body.
            body = soup.find("main") or soup.body or soup
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
            return

        for i, block in enumerate(blocks):
            text = plain_text(block).strip()
            if not text:
                continue
            anchor = block.get("id") or f"idx:{i}"
            meta = value.metadata.set(ReaderKeys.DOC_TYPE, self.DOC_TYPE)
            if title:
                meta = meta.set(ReaderKeys.PAGE_TITLE, title)
            yield Section(
                source_id=value.source_id,
                content=text,
                anchor=anchor,
                order=i,
                metadata=meta,
            )

    @classmethod
    def _collect_top_level(cls, soup) -> list:
        """Собирает `<article>`/`<section>`, не имеющие предка того же типа."""
        all_blocks = soup.find_all(list(cls.SEMANTIC_TAGS))
        return [b for b in all_blocks if not b.find_parent(list(cls.SEMANTIC_TAGS))]


class HtmlReadabilityReader(_HtmlBase, Reader[str]):
    """
    Content-extraction reader: выкидывает boilerplate (nav/header/footer/sidebar)
    через `trafilatura`, потом отдаёт main content одной Section.

    **Когда применять**:
    - произвольный веб (новости, блоги, статьи в открытом интернете)
    - страницы с шумом: меню, хлебные крошки, related links, реклама
    - случаи где heading-структура не выживает после очистки

    **Зависимость**: требует `trafilatura` (опциональная). При отсутствии — `ImportError`
    с понятным сообщением. Установка: `pip install trafilatura` или
    `pip install boba-html[readability]`.

    **Пример**:
    ```python
    reader = HtmlReadabilityReader()
    raw = RawDocument(
        handle=BytesIO(
            b"<html><body>"
            b"<nav>Menu | Home | About</nav>"             # будет выкинуто
            b"<article><h1>News</h1><p>main story</p></article>"
            b"<footer>(c) 2026</footer>"                  # будет выкинуто
            b"</body></html>"
        ),
        source_id=SourceId("https://example.com/news/1"),
    )

    list(reader.convert(raw)) == [
        Section(
            source_id=SourceId("https://example.com/news/1"),  # pass
            content="News\\nmain story",                       # новое: только main content
            anchor=None,                                       # новое
            order=0,                                           # новое
            metadata=Metadata.empty().set(ReaderKeys.DOC_TYPE, "html"),  # merge: + DOC_TYPE
        ),
    ]
    ```
    """  # noqa: E501

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

        # Title берём через bs4 (стандартный путь во всех остальных readers),
        # это надёжнее чем trafilatura.extract_metadata и не делает второй парс.
        soup = parse_html(payload)
        self._strip_noise(soup)
        title = self._extract_title(soup)

        meta = value.metadata.set(ReaderKeys.DOC_TYPE, self.DOC_TYPE)
        if title:
            meta = meta.set(ReaderKeys.PAGE_TITLE, title)

        yield Section(
            source_id=value.source_id,
            content=text.strip(),
            anchor=None,
            order=0,
            metadata=meta,
        )


