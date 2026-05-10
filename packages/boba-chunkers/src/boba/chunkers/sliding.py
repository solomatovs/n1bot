"""
SlidingChunker — фабрика с cекций SectionChunker[str] где нет anchor (заголовка)

Подходит для плоских документов без явных heading:
    raw text
    code
    log-файлы
"""

from __future__ import annotations

from dataclasses import dataclass

from boba.indexing import (
    ChunkerId,
    DigestPrefix,
    FixedDigestPrefix,
    KeyEncoder,
    RecursiveCharSplitter,
    SectionChunker,
    Sha256TextEncoder,
    SourceBasedChunkId,
)

__all__ = [
    "DEFAULT_CHUNK_OVERLAP",
    "DEFAULT_CHUNK_SIZE",
    "DEFAULT_DIGEST_PREFIX_CHARS",
    "SlidingChunkerConfig",
    "sliding_chunker",
]


DEFAULT_CHUNK_SIZE = 1000
DEFAULT_CHUNK_OVERLAP = 200
DEFAULT_DIGEST_PREFIX_CHARS = 12


@dataclass(frozen=True)
class SlidingChunkerConfig:
    """Конфиг sliding_chunker'а."""

    chunk_size: int = DEFAULT_CHUNK_SIZE
    chunk_overlap: int = DEFAULT_CHUNK_OVERLAP
    digest_prefix_chars: int = DEFAULT_DIGEST_PREFIX_CHARS


def sliding_chunker(
    config: SlidingChunkerConfig,
    *,
    encoder: KeyEncoder[str] | None = None,
    prefix: DigestPrefix | None = None,
) -> SectionChunker[str]:
    return SectionChunker(
        chunker_id=ChunkerId("sliding"),
        splitter=RecursiveCharSplitter(
            chunk_size=config.chunk_size,
            chunk_overlap=config.chunk_overlap,
        ),
        id_strategy=SourceBasedChunkId(
            encoder=encoder if encoder is not None else Sha256TextEncoder(),
            prefix=(
                prefix
                if prefix is not None
                else FixedDigestPrefix(config.digest_prefix_chars)
            ),
        ),
    )
