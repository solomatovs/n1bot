"""heading_chunker: фабрика SectionChunker'а с anchor-based chunk_id."""

from __future__ import annotations

from dataclasses import dataclass

from boba.chunking.splitter import TextSplitter
from boba.indexing import ChunkerId
from boba.indexing.chunk_id import AnchorBasedChunkId
from boba.indexing.section_chunker import SectionChunker

__all__ = [
    "DEFAULT_CHUNK_OVERLAP",
    "DEFAULT_CHUNK_SIZE",
    "HeadingChunkerConfig",
    "heading_chunker",
]


DEFAULT_CHUNK_SIZE = 1500
DEFAULT_CHUNK_OVERLAP = 150


@dataclass(frozen=True)
class HeadingChunkerConfig:
    chunk_size: int = DEFAULT_CHUNK_SIZE
    chunk_overlap: int = DEFAULT_CHUNK_OVERLAP


def heading_chunker(config: HeadingChunkerConfig) -> SectionChunker:
    """SectionChunker: char-based split + anchor-based chunk_id (deep-link сохранён)."""
    return SectionChunker(
        chunker_id=ChunkerId("ext.heading"),
        splitter=TextSplitter(
            chunk_size=config.chunk_size,
            chunk_overlap=config.chunk_overlap,
        ),
        id_strategy=AnchorBasedChunkId(),
    )
