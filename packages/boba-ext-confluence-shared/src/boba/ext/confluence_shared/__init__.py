"""Shared Confluence parser: heading-aware HTML processing.

Pure functions без I/O — работают с готовым `BeautifulSoup`/`bytes`/`str`.
Делятся между:
- `boba-ext-confluence-tools` (read-tools для агента: outline / section).
- `boba-ext-confluence-reader` (heading-aware Reader для индексации).
- `boba-ext-confluence-source` (через Reader при индексации).
"""

from __future__ import annotations

from boba.ext.confluence_shared.parse import (
    Heading,
    anchor_for,
    collect_headings,
    parse_html,
    resolve_anchor,
    strip_confluence_macros,
)

__all__ = [
    "Heading",
    "anchor_for",
    "collect_headings",
    "parse_html",
    "resolve_anchor",
    "strip_confluence_macros",
]
