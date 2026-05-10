"""HTML reader'ы: HTML → Section[str].

Несколько вариантов резки HTML, под разные типы документов:

- `HtmlHeadingReader`      — режет по `<h1>..<h6>`. Дефолт для wiki/доков.
- `HtmlPlainReader`        — весь body как одна Section (+ `<title>`).
- `HtmlSemanticReader`     — режет по top-level `<article>`/`<section>` (HTML5).
- `HtmlReadabilityReader`  — content-extraction через `trafilatura`
  (boilerplate-removal); на выходе plain text.
- `HtmlMarkdownifyReader`  — конвертирует HTML в Markdown через `markdownify`,
  затем делегирует резку `MarkdownReader` (выходные Section'ы — markdown-текст).

Все HTML-reader'ы:
- удаляют `<script>`/`<style>` из выдачи;
- кладут `<title>` (если есть) в `ReaderKeys.PAGE_TITLE`;
- пробрасывают `RawDocument.source_id` и мержат `RawDocument.metadata` в Section.
"""

from __future__ import annotations

from collections.abc import Iterable
from io import BytesIO
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
from boba.reader.markdown import MarkdownReader

try:
    import trafilatura as _trafilatura
except ImportError:  # optional dependency — see HtmlReadabilityReader
    _trafilatura = None  # type: ignore[assignment]

try:
    from markdownify import markdownify as _markdownify
except ImportError:  # optional dependency — see HtmlMarkdownifyReader
    _markdownify = None  # type: ignore[assignment]

__all__ = [
    "HtmlHeadingReader",
    "HtmlMarkdownifyReader",
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
    `pip install boba-reader-html[readability]`.

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
                "`pip install boba-reader-html[readability]`."
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


class HtmlMarkdownifyReader(_HtmlBase, Reader[str]):
    """
    HTML → Markdown через `markdownify`, потом heading-aware split через `MarkdownReader`.

    На выходе — `Section[str]` с **markdown**-контентом (DOC_TYPE = "markdown",
    как у обычного `MarkdownReader`). Anchor — slug текста heading'а из конвертированного
    markdown (lowercase, пробелы → `-`); fallback `idx:N`.

    **Когда применять**:
    - LLM-pipeline где нужен markdown-входной формат (структура сохраняется лучше, чем у
      plain text: списки, таблицы, ссылки, code-блоки).
    - HTML-документы с разнообразной разметкой, не покрываемой одной h1..h6 структурой.
    - В связке с `heading_chunker` — режет аккуратно по заголовкам markdown.

    **Когда НЕ применять**:
    - HTML без структуры — `markdownify` всё равно даст один блок plain text;
      проще `HtmlPlainReader` или `HtmlReadabilityReader`.
    - Если нужен plain text — `HtmlReadabilityReader` (`trafilatura`) обычно даёт чище.

    **Зависимости**: требует `markdownify` (опциональная). При отсутствии — `ImportError`.
    Установка: `pip install markdownify` или `pip install boba-reader-html[markdownify]`.

    **Кастомизация**: `markdownify_options` — kwargs для `markdownify.markdownify(...)`,
    например `{"heading_style": "ATX", "strip": ["a"]}`.

    **Pipeline-цепочка**:
    ```
    Transport (RawDocument: HTML bytes)
        ↓
    HtmlMarkdownifyReader  ← конвертит HTML → Markdown через markdownify,
                              затем делегирует MarkdownReader
        ↓ Section[str] (markdown content + heading-anchor)
    SectionChunker via heading_chunker
        ↓ Chunk[str]
    ```

    **Пример** (содержательный HTML с heading'ами, inline-формат, списком, code-тегом
    — markdownify сохраняет всё в markdown-разметке):
    ```python
    reader = HtmlMarkdownifyReader()
    raw = RawDocument(
        handle=BytesIO(
            b"<html><body>"
            b"<h1>Setup</h1>"
            b"<p>To <strong>install</strong>, run the following:</p>"
            b"<ul><li>Step one</li><li>Step two</li></ul>"
            b"<h2>Configure</h2>"
            b"<p>Edit <code>config.yaml</code> to suit your needs.</p>"
            b"</body></html>"
        ),
        source_id=SourceId("doc1"),
    )

    # markdownify конвертит:
    #   <strong>install</strong> → **install**
    #   <ul><li>...</li></ul>    → * ...
    #   <code>config.yaml</code> → `config.yaml`
    #   <h1>/<h2>                → # / ## (heading_style="ATX")
    # Затем MarkdownReader режет по `# ` / `## ` на 2 Section'и.
    list(reader.convert(raw)) == [
        Section(
            source_id=SourceId("doc1"),                          # pass из RawDocument
            content=(                                            # новое: markdown с сохранённой разметкой
                "# Setup\\n\\n"
                "To **install**, run the following:\\n\\n"        # ← <strong> сохранён
                "* Step one\\n* Step two"                         # ← <ul><li> → "* "
            ),
            anchor="setup",                                      # новое: slug("Setup") → "setup"
            order=1,                                             # новое: позиция в документе
            metadata=(                                           # merge: + DOC_TYPE / HEADING_*
                Metadata.empty()
                .set(ReaderKeys.DOC_TYPE, "markdown")            # на выходе уже markdown
                .set(MarkdownKeys.HEADING_LEVEL, 1)
                .set(MarkdownKeys.HEADING_TEXT, "Setup")
            ),
        ),
        Section(
            source_id=SourceId("doc1"),
            content=(
                "## Configure\\n\\n"
                "Edit `config.yaml` to suit your needs."         # ← <code> → backticks
            ),
            anchor="configure",
            order=2,
            metadata=(
                Metadata.empty()
                .set(ReaderKeys.DOC_TYPE, "markdown")
                .set(MarkdownKeys.HEADING_LEVEL, 2)
                .set(MarkdownKeys.HEADING_TEXT, "Configure")
            ),
        ),
    ]
    ```

    **Что показывает пример**:
    - Inline-форматирование (`<strong>`/`<code>`) переходит в markdown
      (`**...**` / `` `...` ``) — это главное преимущество над plain-text-readers.
    - Списки сохраняются как markdown-списки.
    - Каждый `<h1>`/`<h2>` становится отдельной Section с anchor из slug.
    - `DOC_TYPE = "markdown"` (а не "html"), потому что реальный content
      Section.content — уже markdown-текст.
    """  # noqa: E501

    READER_ID: ClassVar[ReaderId] = ReaderId("ext.html.markdownify")

    def __init__(self, *, markdownify_options: dict | None = None) -> None:
        # heading_style="ATX" — обязателен: MarkdownReader парсит только ATX
        # (`# Title`), а у markdownify дефолт Setext (`Title\n=====`),
        # который для нашего pipeline'а не сработает.
        self._markdownify_options = {
            "heading_style": "ATX",
            **(markdownify_options or {}),
        }
        self._md_reader = MarkdownReader()

    def name(self) -> str:
        return "HtmlMarkdownifyReader"

    def reader_id(self) -> ReaderId:
        return self.READER_ID

    def convert(self, value: RawDocument) -> Iterable[Section[str]]:
        if _markdownify is None:
            raise ImportError(
                "HtmlMarkdownifyReader requires `markdownify`. "
                "Install: `pip install markdownify` or "
                "`pip install boba-reader-html[markdownify]`."
            )

        payload = value.handle.read()
        if not payload.strip():
            return

        html_text = payload.decode("utf-8", errors="replace")
        md_text = _markdownify(html_text, **self._markdownify_options)
        if not md_text.strip():
            return

        # Делегируем парсинг markdown'а готовому MarkdownReader.
        # source_id и metadata пробрасываются как есть; MarkdownReader сам
        # проставит ReaderKeys.DOC_TYPE = "markdown" и MarkdownKeys.HEADING_*.
        md_raw = RawDocument(
            handle=BytesIO(md_text.encode("utf-8")),
            source_id=value.source_id,
            metadata=value.metadata,
        )
        yield from self._md_reader.convert(md_raw)


class HtmlExtractedMarkdownifyReader(_HtmlBase, Reader[str]):
    """
    `trafilatura` + `markdownify` + `MarkdownReader` в одной цепочке.

    Pipeline:
    ```
    HTML bytes
        ↓ trafilatura.extract(output_format="html")  ← убирает nav/header/footer
    main content (HTML без boilerplate)
        ↓ markdownify(heading_style="ATX")
    Markdown text
        ↓ MarkdownReader.convert
    Section[str]   (markdown content + heading-anchor)
    ```

    **Когда применять**:
    - Произвольный веб (новости, блоги, статьи) с большим количеством шума
      (меню / реклама / footer) — нужно сначала очистить, потом аккуратно
      переложить в markdown с сохранением списков, ссылок, code-блоков.
    - Альтернативно: `HtmlReadabilityReader` (тоже использует `trafilatura`)
      выдаёт **plain text** с `output_format="txt"`. Этот reader выдаёт
      **markdown** через явный `markdownify`-конвертер — что обычно даёт
      более чистую разметку чем `trafilatura.extract(output_format="markdown")`,
      особенно для списков и таблиц.

    **Когда НЕ применять**:
    - Чистые HTML без boilerplate — `HtmlMarkdownifyReader` достаточен и не
      тянет вторую зависимость.
    - Если markdown-вывод не нужен — `HtmlReadabilityReader` (plain text).

    **Зависимости**: требует **обе** опциональные библиотеки —
    `trafilatura` и `markdownify`. Установка:
    `pip install boba-reader-html[readability,markdownify]`.

    **Кастомизация** (`markdownify_options`): kwargs для `markdownify(...)`,
    например `{"strip": ["a"], "heading_style": "ATX_CLOSED"}`.

    **Важный нюанс про inline-форматирование**: `trafilatura.extract(output_format="html")`
    при очистке стриппит inline-теги (`<strong>`/`<em>`/`<code>`), оставляя только
    block-структуру (heading'и, параграфы, списки). На выходе **bold/italic/inline-code
    не выживают** — markdownify получает уже plain block HTML без них. Если важно
    сохранить inline-разметку — используй `HtmlMarkdownifyReader` (без trafilatura-чистки).

    **Пример** (HTML с nav/footer-шумом и block-структурой; trafilatura отбрасывает
    boilerplate, markdownify даёт markdown с heading'ами и списками):
    ```python
    reader = HtmlExtractedMarkdownifyReader()
    raw = RawDocument(
        handle=BytesIO(
            b"<html><body>"
            b"<nav>Home | About | Blog | Contact</nav>"          # будет выкинуто
            b"<article>"
            b"<h1>Quick start guide</h1>"
            b"<p>Follow these steps to set up your environment quickly.</p>"
            b"<ul>"
            b"<li>Download the package</li>"
            b"<li>Run the installer script</li>"
            b"<li>Configure settings file</li>"
            b"</ul>"
            b"<h2>Next steps</h2>"
            b"<p>Read the full documentation for advanced configuration options.</p>"
            b"</article>"
            b"<footer>Copyright 2026 example.com</footer>"       # будет выкинуто
            b"</body></html>"
        ),
        source_id=SourceId("https://example.com/doc"),
    )

    # 1. trafilatura → main content без nav/footer (HTML с <h1>/<h2>/<p>/<ul>).
    # 2. markdownify → ATX-markdown (`# Title`, `* item`, ...).
    # 3. MarkdownReader → Section'и по heading'ам.
    list(reader.convert(raw)) == [
        Section(
            source_id=SourceId("https://example.com/doc"),       # pass из RawDocument
            content=(                                            # новое: markdown
                "# Quick start guide\\n\\n"                       # heading сохранён
                "Follow these steps to set up your environment quickly.\\n\\n"
                "* Download the package\\n"                       # markdown-список
                "* Run the installer script\\n"
                "* Configure settings file"
            ),
            anchor="quick-start-guide",                          # новое: slug многословного heading'а
            order=1,
            metadata=(                                           # merge: + DOC_TYPE / HEADING_*
                Metadata.empty()
                .set(ReaderKeys.DOC_TYPE, "markdown")            # на выходе markdown
                .set(MarkdownKeys.HEADING_LEVEL, 1)
                .set(MarkdownKeys.HEADING_TEXT, "Quick start guide")
            ),
        ),
        Section(
            source_id=SourceId("https://example.com/doc"),
            content=(
                "## Next steps\\n\\n"
                "Read the full documentation for advanced configuration options."
            ),
            anchor="next-steps",
            order=2,
            metadata=(
                Metadata.empty()
                .set(ReaderKeys.DOC_TYPE, "markdown")
                .set(MarkdownKeys.HEADING_LEVEL, 2)
                .set(MarkdownKeys.HEADING_TEXT, "Next steps")
            ),
        ),
    ]
    ```

    **Что показывает пример**:
    - `<nav>` («Home | About | Blog | Contact») и `<footer>` («Copyright 2026 …»)
      выкинуты trafilatura — в Section'и не попали.
    - Heading'и и списки сохранились через markdownify-конвертацию.
    - Anchor'ы — slug'и многословных heading'ов: «Quick start guide» → `quick-start-guide`.
    - На выходе DOC_TYPE = "markdown", потому что `Section.content` — уже markdown-текст.
    """

    READER_ID: ClassVar[ReaderId] = ReaderId("ext.html.extracted_markdownify")

    def __init__(self, *, markdownify_options: dict | None = None) -> None:
        self._markdownify_options = {
            "heading_style": "ATX",
            **(markdownify_options or {}),
        }
        self._md_reader = MarkdownReader()

    def name(self) -> str:
        return "HtmlExtractedMarkdownifyReader"

    def reader_id(self) -> ReaderId:
        return self.READER_ID

    def convert(self, value: RawDocument) -> Iterable[Section[str]]:
        if _trafilatura is None or _markdownify is None:
            missing = []
            if _trafilatura is None:
                missing.append("trafilatura")
            if _markdownify is None:
                missing.append("markdownify")
            raise ImportError(
                f"HtmlExtractedMarkdownifyReader requires {' + '.join(missing)}. "
                "Install: `pip install boba-reader-html[readability,markdownify]`."
            )

        payload = value.handle.read()
        if not payload.strip():
            return

        # 1. trafilatura: вытащить main content в виде HTML (без nav/footer).
        clean_html = _trafilatura.extract(
            payload, output_format="html", include_comments=False
        )
        if not clean_html:
            return

        # 2. markdownify: HTML → Markdown с ATX-heading'ами.
        md_text = _markdownify(clean_html, **self._markdownify_options)
        if not md_text.strip():
            return

        # 3. MarkdownReader: режет markdown по heading'ам в Section'ы.
        md_raw = RawDocument(
            handle=BytesIO(md_text.encode("utf-8")),
            source_id=value.source_id,
            metadata=value.metadata,
        )
        yield from self._md_reader.convert(md_raw)
