"""HTML → Markdown композитные Reader'ы.

- `HtmlMarkdownifyReader`          — markdownify(HTML) → MarkdownReader.
- `HtmlExtractedMarkdownifyReader` — trafilatura(HTML) → markdownify → MarkdownReader.

Оба наследуют `_HtmlBase` (noise-фильтр / title-extraction) из
`boba.html.reader` и переиспользуют готовый `MarkdownReader` для
финального разбиения markdown'а на Section'и.
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
from boba.html.parser import parse_html
from boba.html.reader import _HtmlBase
from boba.markdown import MarkdownReader

try:
    import trafilatura as _trafilatura
except ImportError:  # optional dependency — see HtmlExtractedMarkdownifyReader
    _trafilatura = None  # type: ignore[assignment]

from markdownify import markdownify as _markdownify

__all__ = [
    "HtmlExtractedMarkdownifyReader",
    "HtmlMarkdownifyReader",
]


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
    - В связке с `markdown_structural_chunker` — режет per-block (heading,
      code-fence, table, list) с structured metadata.

    **Когда НЕ применять**:
    - HTML без структуры — `markdownify` всё равно даст один блок plain text;
      проще `HtmlPlainReader` или `HtmlReadabilityReader`.
    - Если нужен plain text — `HtmlReadabilityReader` (`trafilatura`) обычно даёт чище.

    **Зависимости**: требует `markdownify` (опциональная). При отсутствии — `ImportError`.
    Установка: `pip install markdownify` или `pip install boba-html[markdownify]`.

    **Кастомизация**: `markdownify_options` — kwargs для `markdownify.markdownify(...)`,
    например `{"heading_style": "ATX", "strip": ["a"]}`.

    **Pipeline-цепочка**:
    ```
    Transport (RawDocument: HTML bytes)
        ↓
    HtmlMarkdownifyReader  ← конвертит HTML → Markdown через markdownify,
                              затем делегирует MarkdownReader
        ↓ Section[str] (markdown content + heading-anchor)
    markdown_structural_chunker (AST-based per-block split)
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
                "`pip install boba-html[markdownify]`."
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
    `pip install boba-html[readability,markdownify]`.

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
                "Install: `pip install boba-html[readability,markdownify]`."
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
