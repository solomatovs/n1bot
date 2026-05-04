"""boba-ext-markdown-parser: heading-aware Markdown parser (ATX style)."""

from __future__ import annotations

from boba.ext.markdown_parser.parse import (
    Heading,
    Section,
    anchor_for,
    collect_headings,
    resolve_anchor,
    slugify,
    split_sections,
)

__all__ = [
    "Heading",
    "Section",
    "anchor_for",
    "collect_headings",
    "resolve_anchor",
    "slugify",
    "split_sections",
]
