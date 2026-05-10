"""boba.chunkers — фабрики SectionChunker[str] с готовыми политиками chunk_id.

Все фабрики возвращают `SectionChunker[str]`, разница — в стратегии
`ChunkIdStrategy`:

- `heading_chunker` — `AnchorBasedChunkId`: chunk_id выводится из
  `(source_id, anchor)`, deep-link стабилен между ре-индексациями
  пока anchor неизменен. Подходит для Markdown / HTML / любых
  документов с явными heading'ами.

- `sliding_chunker` — `SourceBasedChunkId`: chunk_id выводится из
  `source_id` без anchor'а. Подходит для плоских документов
  (raw text, code) — там anchor отсутствует или нестабилен.

Splitter в обеих фабриках — `RecursiveCharSplitter` из `boba.indexing`
(soft-break по separator'ам, lazy yield, offset-tracking для
`ChunkLocation`). Конфиг chunk_size / chunk_overlap / digest_prefix
передаётся через `*ChunkerConfig` dataclass.
"""

from __future__ import annotations

from boba.chunkers.heading import (
    DEFAULT_CHUNK_OVERLAP as DEFAULT_HEADING_CHUNK_OVERLAP,
)
from boba.chunkers.heading import (
    DEFAULT_CHUNK_SIZE as DEFAULT_HEADING_CHUNK_SIZE,
)
from boba.chunkers.heading import (
    HeadingChunkerConfig,
    heading_chunker,
)
from boba.chunkers.sliding import (
    DEFAULT_CHUNK_OVERLAP as DEFAULT_SLIDING_CHUNK_OVERLAP,
)
from boba.chunkers.sliding import (
    DEFAULT_CHUNK_SIZE as DEFAULT_SLIDING_CHUNK_SIZE,
)
from boba.chunkers.sliding import (
    SlidingChunkerConfig,
    sliding_chunker,
)

__all__ = [
    "DEFAULT_HEADING_CHUNK_OVERLAP",
    "DEFAULT_HEADING_CHUNK_SIZE",
    "DEFAULT_SLIDING_CHUNK_OVERLAP",
    "DEFAULT_SLIDING_CHUNK_SIZE",
    "HeadingChunkerConfig",
    "SlidingChunkerConfig",
    "heading_chunker",
    "sliding_chunker",
]
