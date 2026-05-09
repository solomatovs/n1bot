"""sliding_chunker: фабрика SectionChunker'а с source-based chunk_id."""

from __future__ import annotations

from dataclasses import dataclass

from boba.chunking.splitter import TextSplitter
from boba.indexing import ChunkerId
from boba.indexing.chunk_id import SourceBasedChunkId
from boba.indexing.section_chunker import SectionChunker

__all__ = [
    "DEFAULT_CHUNK_OVERLAP",
    "DEFAULT_CHUNK_SIZE",
    "SlidingChunkerConfig",
    "sliding_chunker",
]


DEFAULT_CHUNK_SIZE = 1000
DEFAULT_CHUNK_OVERLAP = 200


@dataclass(frozen=True)
class SlidingChunkerConfig:
    chunk_size: int = DEFAULT_CHUNK_SIZE
    chunk_overlap: int = DEFAULT_CHUNK_OVERLAP


def sliding_chunker(config: SlidingChunkerConfig) -> SectionChunker:
    """SectionChunker: char-based split + source-based chunk_id (без anchor)."""
    return SectionChunker(
        chunker_id=ChunkerId("ext.sliding"),
        splitter=TextSplitter(
            chunk_size=config.chunk_size,
            chunk_overlap=config.chunk_overlap,
        ),
        id_strategy=SourceBasedChunkId(),
    )
