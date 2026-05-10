"""boba.markdown — всё для индексации markdown-формата.

Один пакет содержит весь markdown-стек:

- `sections.py` — pure ATX-section parser (`split_sections`, `slugify`,
  `anchor_for`, ...). Используется `MarkdownReader` для разбиения на
  логические секции по heading'ам.
- `blocks.py` — pure block-AST parser через `markdown-it-py`
  (`MarkdownBlockParser`). Используется `MarkdownStructuralChunker`.
  Опциональная зависимость `markdown-it-py`.
- `reader.py` — `MarkdownReader`: `RawDocument` → `Section[str]` по ATX-heading'ам.
- `chunker.py` — `MarkdownStructuralChunker`: `Section[str]` → `Chunk[str]`
  с per-block стратегиями (heading-prefix-merge, atomic code-fence/table/list,
  paragraph char-split с overlap).
- `keys.py` — `MarkdownKeys` с metadata-ключами reader'а.

Типы блоков — в `boba.indexing` (sealed `Block` иерархия).

Зависимости:
- `boba-indexing` (Reader / Chunker / Splitter / Section / Chunk / Block / OverlapCharSplitter).
- `markdown-it-py` (опциональная). Установка:
  `pip install boba-markdown[structural]`.
"""

from __future__ import annotations

from boba.markdown.blocks import MarkdownBlockParser
from boba.markdown.chunker import (
    DEFAULT_CHUNK_OVERLAP,
    DEFAULT_CHUNK_SIZE,
    DEFAULT_DIGEST_PREFIX_CHARS,
    MarkdownStructuralChunker,
    MarkdownStructuralChunkerConfig,
    MarkdownStructuralKeys,
    markdown_structural_chunker,
)
from boba.markdown.keys import MarkdownKeys
from boba.markdown.reader import MarkdownReader
from boba.markdown.sections import (
    Heading,
    MarkdownSection,
    anchor_for,
    collect_headings,
    resolve_anchor,
    slugify,
    split_sections,
)

__all__ = [
    "DEFAULT_CHUNK_OVERLAP",
    "DEFAULT_CHUNK_SIZE",
    "DEFAULT_DIGEST_PREFIX_CHARS",
    "Heading",
    "MarkdownBlockParser",
    "MarkdownKeys",
    "MarkdownReader",
    "MarkdownSection",
    "MarkdownStructuralChunker",
    "MarkdownStructuralChunkerConfig",
    "MarkdownStructuralKeys",
    "anchor_for",
    "collect_headings",
    "markdown_structural_chunker",
    "resolve_anchor",
    "slugify",
    "split_sections",
]
