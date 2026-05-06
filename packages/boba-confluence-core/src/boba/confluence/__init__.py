"""boba-confluence-core: домен Confluence (parse + connection + reader + decoder).

Чистый домен без I/O-зависимостей: HTML-парсинг, конфиг-схема (без httpx),
JSON-декодер и heading-aware Reader. httpx-фабрики живут в ext-пакетах.
"""

from __future__ import annotations

from boba.confluence.connection import (
    ConfluenceConnection,
    ConfluenceConnectionConfig,
)
from boba.confluence.decoder import ConfluenceJsonDecoder
from boba.confluence.parse import (
    Heading,
    anchor_for,
    collect_headings,
    is_confluence_macro,
    parse_html,
    plain_text,
    resolve_anchor,
    strip_confluence_macros,
    text_between,
)
from boba.confluence.reader import ConfluenceReader

__all__ = [
    "ConfluenceConnection",
    "ConfluenceConnectionConfig",
    "ConfluenceJsonDecoder",
    "ConfluenceReader",
    "Heading",
    "anchor_for",
    "collect_headings",
    "is_confluence_macro",
    "parse_html",
    "plain_text",
    "resolve_anchor",
    "strip_confluence_macros",
    "text_between",
]
