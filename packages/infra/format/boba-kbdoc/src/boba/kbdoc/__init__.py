"""boba.kbdoc — формат KB-документа поверх произвольного inner Reader[str].

Содержимое:

- `reader.py` — `KbDocReader(inner: Reader[str])`: парсит header
  (`# Title`, `**tags:**`, `**source:**`, `**anchor:**`, `---`) и
  делегирует body во вложенный Reader (`MarkdownReader`,
  `PlainTextReader`, `HtmlReader` — что угодно).
- `keys.py`   — `KbDocKeys`: typed `MetadataKey`-и (`SOURCE_URL` —
  top-level для citation; `CUSTOM_PREFIX="reader.kbdoc."` для
  произвольных header-ключей).

Зависимости:

- `boba-indexing` — `Reader[str]`, `Section`, `Metadata`, `MetadataKey`.
"""

from __future__ import annotations

from boba.kbdoc.keys import KbDocKeys
from boba.kbdoc.reader import KbDocReader, ParsedKbDocHeader

__all__ = [
    "KbDocKeys",
    "KbDocReader",
    "ParsedKbDocHeader",
]
