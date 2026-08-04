"""boba.text — text-pipeline impl'ы доменных контрактов из boba.indexing.

Содержит конкретные реализации, которые предполагают text-content (T = str),
но не привязаны к конкретному формату (markdown, html и т.п.).

Содержимое:

- splitter.py — OverlapCharSplitter: char-based рекурсивный split с
  overlap'ом. Реализация Splitter[str].
- chunker.py  — SectionChunker: композиция Splitter[T] +
  ChunkIdStrategy[T] -> Chunker[T]. API generic в T, но pipeline-модель
  «splitter режет content на char-offset куски» естественна для текста.
- structural_chunker.py — StructuralChunker: heading-aware Chunker[str],
  потребляет Section.to_format_plan(), реплицирует header в каждый чанк
  секции, ведёт стек breadcrumbs per source_id.
- reader.py   — PlainTextReader: тривиальный Reader[str] (decode ->
  один Section со всем текстом).

Зависимости: boba-indexing.
"""

from __future__ import annotations

from boba.text.chunker import SectionChunker
from boba.text.document import (
    DocumentMedia,
    LiteParseError,
    LiteParseParams,
    PagedDocumentReader,
    PageSectionBuilder,
    ParsedPage,
)
from boba.text.reader import PlainTextReader
from boba.text.splitter import OverlapCharSplitter
from boba.text.structural_chunker import SplitterFactory, StructuralChunker

__all__ = [
    "DocumentMedia",
    "LiteParseError",
    "LiteParseParams",
    "OverlapCharSplitter",
    "PageSectionBuilder",
    "PagedDocumentReader",
    "ParsedPage",
    "PlainTextReader",
    "SectionChunker",
    "SplitterFactory",
    "StructuralChunker",
]
