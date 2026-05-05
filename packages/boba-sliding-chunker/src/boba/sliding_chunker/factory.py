"""SlidingChunkerFactory: AppConfig → SlidingChunker."""

from __future__ import annotations

from boba.indexing import (
    Chunker,
    ChunkerFactory,
    ChunkerId,
    IndexerExtensionContext,
)
from boba.sliding_chunker.chunker import SlidingChunker
from boba.sliding_chunker.config import SlidingChunkerConfigSection

__all__ = ["SlidingChunkerFactory"]


class SlidingChunkerFactory(ChunkerFactory):
    def id(self) -> ChunkerId:
        return ChunkerId("ext.sliding")

    def produce(self, ctx: IndexerExtensionContext) -> Chunker:
        cfg = ctx.config.section(SlidingChunkerConfigSection)
        return SlidingChunker(cfg)
