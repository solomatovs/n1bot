"""boba.reader.html — HTML reader для индексации.

`HtmlReader` режет HTML-документ на heading-aware секции через `<h1>..<h6>`.
Anchor каждой секции — html-`id` heading-тега или fallback `idx:N`.
`<script>`/`<style>` отфильтровываются. `<title>` (если есть) попадает
в `ReaderKeys.PAGE_TITLE` каждой секции.

Pure parser (`parser.py`) экспонирует heading-collection / text-extraction
отдельно — для расширений под конкретные HTML-диалекты (Confluence
`ac:structured-macro` и т.п.) можно переопределять `text_extractor` /
`anchor_extractor` в `collect_headings(...)`.

Использование:
    reader = HtmlReader()
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
from boba.reader.html.reader import HtmlReader

__all__ = [
    "Heading",
    "HtmlKeys",
    "HtmlReader",
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
