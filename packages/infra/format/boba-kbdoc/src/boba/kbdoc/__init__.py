"""boba.kbdoc — формат KB-документа (один файл = одна Section).

Содержимое:

- `reader.py` — `KbDocReader`: парсит header (`# Title`, `**tags:**`,
  `**source:**`, `**anchor:**`, `---`) → metadata; body отдаёт одной
  `ParagraphSection` без структурной разбивки (операторская KB-конвенция:
  каждый документ — атомарная единица). Размерный fallback делает
  splitter в `StructuralChunker`.
- `keys.py`   — `KbDocKeys`: typed `MetadataKey`-и (`SOURCE_URL` —
  top-level для citation; `CUSTOM_PREFIX="reader.kbdoc."` для
  произвольных header-ключей).

Зависимости:

- `boba-indexing` — `Reader[str]`, `Section`, `ParagraphSection`,
  `Metadata`, `MetadataKey`.
"""

from __future__ import annotations

from boba.kbdoc.keys import KbDocKeys
from boba.kbdoc.reader import KbDocReader, ParsedKbDocHeader

__all__ = [
    "KbDocKeys",
    "KbDocReader",
    "ParsedKbDocHeader",
]
