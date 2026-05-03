"""HeadingChunkerFactory: AppConfig → HeadingChunker."""

from __future__ import annotations

from boba.ext.heading_chunker.chunker import HeadingChunker
from boba.ext.heading_chunker.config import HeadingChunkerConfigSection
from boba.indexing import (
    Chunker,
    ChunkerFactory,
    ChunkerId,
    IndexerExtensionContext,
)

__all__ = ["HeadingChunkerFactory"]


class HeadingChunkerFactory(ChunkerFactory):
    def id(self) -> ChunkerId:
        return ChunkerId("ext.heading")

    def produce(self, ctx: IndexerExtensionContext) -> Chunker:
        cfg = ctx.config.section(HeadingChunkerConfigSection)
        return HeadingChunker(cfg)
