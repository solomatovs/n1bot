"""boba.markdown — markdown-формат для индексации.

Содержимое:

- parser.py   — MarkdownSectionParser: parsing markdown через
  markdown-it-py AST -> поток типизированных Section-ов.
- reader.py   — MarkdownReader: тонкий wrapper над парсером.
- sections.py — markdown-специфичные Section-типы:
  MarkdownCodeFenceSection, MarkdownTableSection, MarkdownListSection,
  MarkdownBlockquoteSection, MarkdownHorizontalRuleSection.
  Доменные HeadingSection / ParagraphSection живут в boba.indexing.
- keys.py     — MarkdownKeys: typed MetadataKey-и для markdown-полей.

Зависимости (обязательные):
- boba-indexing — Section[T] + базовые типы.
- markdown-it-py>=3.0 — block-level AST.
"""

from __future__ import annotations

from boba.markdown.keys import MarkdownKeys
from boba.markdown.parser import MarkdownSectionParser, slugify
from boba.markdown.reader import MarkdownReader
from boba.markdown.sections import (
    MarkdownBlockquoteSection,
    MarkdownCodeFenceSection,
    MarkdownHorizontalRuleSection,
    MarkdownListSection,
    MarkdownTableSection,
)

__all__ = [
    "MarkdownBlockquoteSection",
    "MarkdownCodeFenceSection",
    "MarkdownHorizontalRuleSection",
    "MarkdownKeys",
    "MarkdownListSection",
    "MarkdownReader",
    "MarkdownSectionParser",
    "MarkdownTableSection",
    "slugify",
]
