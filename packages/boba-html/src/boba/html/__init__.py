"""boba.html — HTML reader'ы для индексации.

Чистые HTML reader'ы без зависимости на markdown-инфраструктуру:

- `HtmlHeadingReader`      — heading-aware split по `<h1>..<h6>`. Дефолт для
  wiki / документации / Confluence; anchor — html-`id` или `idx:N`.
- `HtmlPlainReader`        — весь body + `<title>` как одна Section. Для HTML
  без структуры (лендинги, e-mail, simple pages).
- `HtmlSemanticReader`     — режет по top-level `<article>`/`<section>` (HTML5).
- `HtmlReadabilityReader`  — content-extraction через `trafilatura` (опциональная
  зависимость; убирает nav/header/footer/sidebar boilerplate); plain text.

Все reader'ы фильтруют `<script>`/`<style>` и кладут `<title>` (если есть)
в `ReaderKeys.PAGE_TITLE`.

**Композитные HTML→Markdown reader'ы** (`HtmlMarkdownifyReader`,
`HtmlExtractedMarkdownifyReader`) живут в отдельном пакете
`boba-html-as-markdown`, чтобы не тащить зависимость на
`boba-markdown` в чистый html-проект.

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

from boba.html.keys import HtmlKeys
from boba.html.parser import (
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
from boba.html.reader import (
    HtmlHeadingReader,
    HtmlPlainReader,
    HtmlReadabilityReader,
    HtmlSemanticReader,
)

__all__ = [
    "Heading",
    "HtmlHeadingReader",
    "HtmlKeys",
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
