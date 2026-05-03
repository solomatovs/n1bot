"""SlidingChunker: режет каждую Section на чанки фиксированного размера."""

from __future__ import annotations

import hashlib
from collections.abc import Iterable

from boba.ext.sliding_chunker.config import SlidingChunkerConfig
from boba.ext.sliding_chunker.splitter import split_text
from boba.indexing import (
    Chunk,
    Chunker,
    ChunkerId,
    IndexingContext,
    Section,
)

__all__ = ["SlidingChunker"]


class SlidingChunker(Chunker):
    """Per-Section sliding chunker. Чанки одного source_id перенумерованы подряд."""

    def __init__(self, config: SlidingChunkerConfig) -> None:
        self._config = config

    def name(self) -> str:
        return f"SlidingChunker(size={self._config.chunk_size})"

    def chunker_id(self) -> ChunkerId:
        return ChunkerId("ext.sliding")

    def stream(
        self, ctx: IndexingContext, stream: Iterable[Section]
    ) -> Iterable[Chunk]:
        del ctx
        per_source_index: dict[str, int] = {}
        for section in stream:
            pieces = split_text(
                section.text,
                chunk_size=self._config.chunk_size,
                chunk_overlap=self._config.chunk_overlap,
            )
            for piece in pieces:
                idx = per_source_index.get(section.source_id, 0)
                per_source_index[section.source_id] = idx + 1
                yield Chunk(
                    chunk_id=_chunk_id(section.source_id, idx),
                    source_id=section.source_id,
                    text=piece,
                    anchor=section.anchor,
                    chunk_index=idx,
                    metadata=dict(section.metadata),
                )


def _chunk_id(source_id: str, chunk_index: int) -> str:
    """Стабильный id чанка: SHA1 от source_id + индекс."""
    digest = hashlib.sha1(
        source_id.encode("utf-8"),
        usedforsecurity=False,
    ).hexdigest()
    return f"{digest[:16]}:{chunk_index}"
