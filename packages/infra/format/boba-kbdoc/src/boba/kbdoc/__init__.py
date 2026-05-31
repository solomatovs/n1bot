"""boba.kbdoc — формат KB-документа (один файл = одна Section).

Содержимое:

- `reader.py` — `KbDocReader`: строго парсит плоский `key: value` header
  до `---` (обязательные `source`/`title`/`page_id`/`space`, иначе
  `KbDocFormatError`) → metadata; body отдаёт одной `ParagraphSection`
  без структурной разбивки (операторская KB-конвенция: каждый документ —
  атомарная единица). Размерный fallback делает splitter в
  `StructuralChunker`.
- `keys.py`   — `KbDocKeys`: typed `MetadataKey`-и (`SOURCE_URL` —
  top-level для citation; `PAGE_ID`/`SPACE` под `reader.kbdoc.*`;
  `CUSTOM_PREFIX="reader.kbdoc."` для произвольных header-ключей).

Зависимости:

- `boba-indexing` — `Reader[str]`, `Section`, `ParagraphSection`,
  `Metadata`, `MetadataKey`.
"""

from __future__ import annotations

from boba.kbdoc.keys import KbDocKeys
from boba.kbdoc.reader import KbDocFormatError, KbDocReader, ParsedKbDocHeader

__all__ = [
    "KbDocFormatError",
    "KbDocKeys",
    "KbDocReader",
    "ParsedKbDocHeader",
]
