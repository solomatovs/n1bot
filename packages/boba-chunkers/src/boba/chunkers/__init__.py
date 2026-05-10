"""boba.chunkers — фабрики SectionChunker[str] с готовыми политиками chunk_id и splitter'ами.

Все фабрики возвращают `SectionChunker[str]`, разница — в комбинации
`Splitter` + `ChunkIdStrategy`:

- `heading_chunker` — `OverlapCharSplitter` (символьная резка) +
  `AnchorBasedChunkId`. chunk_id выводится из `(source_id, anchor)`,
  deep-link стабилен между ре-индексациями пока anchor неизменен.
  Подходит для документов с явными heading'ами и обычным prose-контентом.

- `markdown_aware_chunker` — `MarkdownAwareSplitter` (markdown-aware
  separators: heading/code-fence/rules → обычные) + `AnchorBasedChunkId`.
  Подходит когда `Section.content` сам содержит markdown-разметку
  (типичный случай: после `HtmlMarkdownifyReader` или `MarkdownReader`).

- `sliding_chunker` — `OverlapCharSplitter` + `SourceBasedChunkId`.
  chunk_id выводится из `source_id` без anchor'а. Подходит для плоских
  документов (raw text, code) — там anchor отсутствует или нестабилен.
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
from boba.chunkers.markdown_aware import (
    DEFAULT_CHUNK_OVERLAP as DEFAULT_MARKDOWN_AWARE_CHUNK_OVERLAP,
)
from boba.chunkers.markdown_aware import (
    DEFAULT_CHUNK_SIZE as DEFAULT_MARKDOWN_AWARE_CHUNK_SIZE,
)
from boba.chunkers.markdown_aware import (
    MarkdownAwareChunkerConfig,
    markdown_aware_chunker,
)
from boba.chunkers.markdown_splitter import MarkdownAwareSplitter
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
    "DEFAULT_MARKDOWN_AWARE_CHUNK_OVERLAP",
    "DEFAULT_MARKDOWN_AWARE_CHUNK_SIZE",
    "DEFAULT_SLIDING_CHUNK_OVERLAP",
    "DEFAULT_SLIDING_CHUNK_SIZE",
    "HeadingChunkerConfig",
    "MarkdownAwareChunkerConfig",
    "MarkdownAwareSplitter",
    "SlidingChunkerConfig",
    "heading_chunker",
    "markdown_aware_chunker",
    "sliding_chunker",
]
