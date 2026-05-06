"""HeadingChunker: per-Section split, anchor наследуется в каждом чанке.

В отличие от sliding-chunker:
- Sub-split происходит **только внутри одной Section** — границы heading'ов
  не пересекаются. Это даёт чистые чанки «один раздел документа».
- `Chunk.anchor` всегда равен `Section.anchor` исходной Section'и — для
  retrieval можно показывать deep-link `source_id#anchor`.
- Section без анкора (anchor=None) обрабатывается как самостоятельный
  text-блок без deep-link.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable

from boba.heading_chunker.config import HeadingChunkerConfig
from boba.indexing import Chunk, Chunker, ChunkerId
from boba.processing import IndexingContext, Section
from boba.text_splitter import TextSplitter

__all__ = ["HeadingChunker"]


class HeadingChunker(Chunker):
    """Section → Chunk; anchor сохранён, sub-split не пересекает Section."""

    def __init__(self, config: HeadingChunkerConfig) -> None:
        self._splitter = TextSplitter(
            chunk_size=config.chunk_size,
            chunk_overlap=config.chunk_overlap,
        )
        self._size = config.chunk_size

    def name(self) -> str:
        return f"HeadingChunker(size={self._size})"

    def chunker_id(self) -> ChunkerId:
        return ChunkerId("ext.heading")

    def stream(
        self, ctx: IndexingContext, stream: Iterable[Section]
    ) -> Iterable[Chunk]:
        del ctx
        per_source_index: dict[str, int] = {}
        for section in stream:
            pieces = self._splitter.convert(section.text)
            for piece in pieces:
                idx = per_source_index.get(section.source_id, 0)
                per_source_index[section.source_id] = idx + 1
                yield Chunk(
                    chunk_id=_chunk_id(section.source_id, section.anchor, idx),
                    source_id=section.source_id,
                    text=piece,
                    anchor=section.anchor,
                    chunk_index=idx,
                    metadata=dict(section.metadata),
                )


def _chunk_id(source_id: str, anchor: str | None, chunk_index: int) -> str:
    """Стабильный id чанка: SHA1 от (source_id, anchor) + индекс."""
    key = f"{source_id}#{anchor or ''}".encode()
    digest = hashlib.sha1(key, usedforsecurity=False).hexdigest()
    return f"{digest[:16]}:{chunk_index}"
