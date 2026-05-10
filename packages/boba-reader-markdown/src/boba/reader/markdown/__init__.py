"""
boba.reader.markdown — Markdown reader для индексации

`MarkdownReader` режет markdown-документ на heading-aware секции
(ATX-стиль: `# Title`, `## Subtitle`, ...).
Каждая секция получает anchor из slug текста heading
anchor стабилен между ре-индексациями
пока заголовок не меняется, что даёт стабильный chunk_id для
`AnchorBasedChunkId` стратегии.

Использование:
    reader = MarkdownReader()
    for raw in transport.stream(ctx, requests):
        for section in reader.convert(raw):
            print(section.anchor, section.content[:50])
"""

from __future__ import annotations

from boba.reader.markdown.keys import MarkdownKeys
from boba.reader.markdown.parser import (
    Heading,
    MarkdownSection,
    anchor_for,
    collect_headings,
    resolve_anchor,
    slugify,
    split_sections,
)
from boba.reader.markdown.reader import MarkdownReader

__all__ = [
    "Heading",
    "MarkdownKeys",
    "MarkdownReader",
    "MarkdownSection",
    "anchor_for",
    "collect_headings",
    "resolve_anchor",
    "slugify",
    "split_sections",
]
