"""boba.markdown — всё для индексации markdown-формата.

Содержимое:

- `parser.py` — `MarkdownSectionParser`: парсит markdown через
  `markdown-it-py` AST → поток типизированных `Section`-ов
  (`HeadingSection`, `ParagraphSection`, `CodeFenceSection`,
  `TableSection`, `ListSection`, `BlockquoteSection`,
  `HorizontalRuleSection`). Также экспортирует helper `slugify`.
- `reader.py` — `MarkdownReader`: тонкий wrapper над `MarkdownSectionParser`,
  декодирует `RawDocument.handle` и заполняет `source_id` / `base_metadata`
  на каждой эмитируемой Section.
- `keys.py` — `MarkdownKeys` (markdown-specific metadata-ключи, если потребуются).

Чанкинг markdown-документов выполняется generic'ом
`boba.indexing.StructuralChunker` (в `boba-indexing`) — он применяет
per-section-type стратегии (heading-prefix-merge, atomic code-fence,
row-by-row split таблиц, item-by-item split списков и т.п.) на основе
`isinstance` типизированных Section'ов.

Зависимости:
- `boba-indexing` (`Section[T]` иерархия + `StructuralChunker`).
- `markdown-it-py` (опциональная). Установка:
  `pip install boba-markdown[structural]`.
"""

from __future__ import annotations

from boba.markdown.keys import MarkdownKeys
from boba.markdown.parser import MarkdownSectionParser, slugify
from boba.markdown.reader import MarkdownReader

__all__ = [
    "MarkdownKeys",
    "MarkdownReader",
    "MarkdownSectionParser",
    "slugify",
]
