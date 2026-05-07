"""boba-ext-html-parser: generic heading-aware HTML parser."""

from __future__ import annotations

from boba.html_parser.parse import (
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

__all__ = [
    "Heading",
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
