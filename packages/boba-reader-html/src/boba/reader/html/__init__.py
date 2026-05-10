"""boba.reader.html — HTML reader'ы для индексации.

Несколько `Reader[str]`-реализаций под разные типы HTML-документов:

- `HtmlHeadingReader`      — heading-aware split по `<h1>..<h6>`. Дефолт для
  wiki / документации / Confluence; anchor — html-`id` или `idx:N`.
- `HtmlPlainReader`        — весь body + `<title>` как одна Section. Для HTML
  без структуры (лендинги, e-mail, simple pages).
- `HtmlSemanticReader`     — режет по top-level `<article>`/`<section>` (HTML5).
- `HtmlReadabilityReader`  — content-extraction через `trafilatura` (опциональная
  зависимость; убирает nav/header/footer/sidebar boilerplate); plain text.
- `HtmlMarkdownifyReader`  — конвертирует HTML в Markdown через `markdownify`
  (опциональная зависимость), затем режет heading-aware через MarkdownReader.
- `HtmlExtractedMarkdownifyReader` — `trafilatura` + `markdownify` подряд:
  сначала очистить boilerplate, потом конвертить main content в markdown.

Все reader'ы фильтруют `<script>`/`<style>` и кладут `<title>` (если есть)
в `ReaderKeys.PAGE_TITLE`.

Pure parser (`parser.py`) экспонирует heading-collection / text-extraction
отдельно — для расширений под конкретные HTML-диалекты (Confluence
`ac:structured-macro` и т.п.) можно переопределять `text_extractor` /
`anchor_extractor` в `collect_headings(...)`.

Использование:
    reader = HtmlHeadingReader()
    for raw in transport.stream(ctx, requests):
        for section in reader.convert(raw):
            print(section.anchor, section.content[:50])
"""

from __future__ import annotations

from boba.reader.html.keys import HtmlKeys
from boba.reader.html.parser import (
    Heading,
    anchor_for,
    collect_headings,
    extract_html_id,
    heading_default_text,
    heading_default_text_skip,
    is_inside_heading,
    parse_html,
    plain_text,
    resolve_anchor,
    text_between,
)
from boba.reader.html.reader import (
    HtmlExtractedMarkdownifyReader,
    HtmlHeadingReader,
    HtmlMarkdownifyReader,
    HtmlPlainReader,
    HtmlReadabilityReader,
    HtmlSemanticReader,
)

__all__ = [
    "Heading",
    "HtmlExtractedMarkdownifyReader",
    "HtmlHeadingReader",
    "HtmlKeys",
    "HtmlMarkdownifyReader",
    "HtmlPlainReader",
    "HtmlReadabilityReader",
    "HtmlSemanticReader",
    "anchor_for",
    "collect_headings",
    "extract_html_id",
    "heading_default_text",
    "heading_default_text_skip",
    "is_inside_heading",
    "parse_html",
    "plain_text",
    "resolve_anchor",
    "text_between",
]
