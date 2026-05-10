"""
HeadingChunker — фабрика секций SectionChunker[str] в которой есть
к чему привязать чанк, некий якорь сообщения - anchor

Подходит для форматов где есть anchor (якорь), это
    Markdown
    HTML
    Confluence storage
"""

from __future__ import annotations

from dataclasses import dataclass

from boba.indexing import (
    AnchorBasedChunkId,
    ChunkerId,
    DigestPrefix,
    KeyEncoder,
    OverlapCharSplitter,
    SectionChunker,
)

__all__ = [
    "DEFAULT_CHUNK_OVERLAP",
    "DEFAULT_CHUNK_SIZE",
    "DEFAULT_DIGEST_PREFIX_CHARS",
    "HeadingChunkerConfig",
    "heading_chunker",
]


DEFAULT_CHUNK_SIZE = 1500
DEFAULT_CHUNK_OVERLAP = 150
DEFAULT_DIGEST_PREFIX_CHARS = 12


@dataclass(frozen=True)
class HeadingChunkerConfig:
    """Конфиг heading_chunker"""

    chunk_size: int = DEFAULT_CHUNK_SIZE
    chunk_overlap: int = DEFAULT_CHUNK_OVERLAP
    digest_prefix_chars: int = DEFAULT_DIGEST_PREFIX_CHARS


def heading_chunker(
    config: HeadingChunkerConfig,
    encoder: KeyEncoder[str],
    prefix: DigestPrefix,
) -> SectionChunker[str]:
    return SectionChunker(
        chunker_id=ChunkerId("heading"),
        splitter=OverlapCharSplitter(
            chunk_size=config.chunk_size,
            chunk_overlap=config.chunk_overlap,
        ),
        id_strategy=AnchorBasedChunkId(
            encoder=encoder,
            prefix=prefix,
        ),
    )
