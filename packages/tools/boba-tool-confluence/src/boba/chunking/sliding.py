"""SlidingChunker: режет каждую Section на чанки фиксированного размера."""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from dataclasses import dataclass

from boba.chunking.splitter import TextSplitter
from boba.indexing import Chunk, Chunker, ChunkerId
from boba.processing import IndexingContext, Section

__all__ = [
    "DEFAULT_CHUNK_OVERLAP",
    "DEFAULT_CHUNK_SIZE",
    "SlidingChunker",
    "SlidingChunkerConfig",
]


DEFAULT_CHUNK_SIZE = 1000
DEFAULT_CHUNK_OVERLAP = 200


@dataclass(frozen=True)
class SlidingChunkerConfig:
    chunk_size: int = DEFAULT_CHUNK_SIZE
    chunk_overlap: int = DEFAULT_CHUNK_OVERLAP


class SlidingChunker(Chunker):
    """Per-Section sliding chunker. Чанки одного source_id перенумерованы подряд."""

    def __init__(self, config: SlidingChunkerConfig) -> None:
        self._splitter = TextSplitter(
            chunk_size=config.chunk_size,
            chunk_overlap=config.chunk_overlap,
        )
        self._size = config.chunk_size

    def name(self) -> str:
        return f"SlidingChunker(size={self._size})"

    def chunker_id(self) -> ChunkerId:
        return ChunkerId("ext.sliding")

    def stream(
        self, ctx: IndexingContext, stream: Iterable[Section]
    ) -> Iterable[Chunk]:
        del ctx
        per_source_index: dict[str, int] = {}
        for section in stream:
            for piece in self._splitter.convert(section.text):
                idx = per_source_index.get(section.source_id, 0)
                per_source_index[section.source_id] = idx + 1
                yield Chunk(
                    chunk_id=self._chunk_id(section.source_id, idx),
                    source_id=section.source_id,
                    text=piece,
                    anchor=section.anchor,
                    chunk_index=idx,
                    metadata=dict(section.metadata),
                )

    @staticmethod
    def _chunk_id(source_id: str, chunk_index: int) -> str:
        """Стабильный id чанка: SHA1 от source_id + индекс."""
        digest = hashlib.sha1(
            source_id.encode("utf-8"),
            usedforsecurity=False,
        ).hexdigest()
        return f"{digest[:16]}:{chunk_index}"
