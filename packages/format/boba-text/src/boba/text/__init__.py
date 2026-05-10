"""boba.text — text-pipeline impl'ы доменных контрактов из `boba.indexing`.

Содержит конкретные реализации, которые предполагают text-content (`T = str`),
но не привязаны к конкретному формату (markdown, html и т.п.).

Содержимое:

- `splitter.py` — `OverlapCharSplitter`: char-based рекурсивный split с
  overlap'ом. Реализация `Splitter[str]`.
- `chunker.py`  — `SectionChunker`: композиция `Splitter[T]` +
  `ChunkIdStrategy[T]` → `Chunker[T]`. API generic в `T`, но pipeline-модель
  «splitter режет content на char-offset куски» естественна для текста.
- `reader.py`   — `PlainTextReader`: тривиальный `Reader[str]` (decode →
  один Section со всем текстом).

Зависимости: `boba-indexing`.
"""

from __future__ import annotations

from boba.text.chunker import SectionChunker
from boba.text.reader import PlainTextReader
from boba.text.splitter import OverlapCharSplitter

__all__ = [
    "OverlapCharSplitter",
    "PlainTextReader",
    "SectionChunker",
]
