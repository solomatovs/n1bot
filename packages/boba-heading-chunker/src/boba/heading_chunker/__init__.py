"""boba-heading-chunker: heading-aware chunker."""

from __future__ import annotations

from boba.heading_chunker.chunker import HeadingChunker
from boba.heading_chunker.config import (
    DEFAULT_CHUNK_OVERLAP,
    DEFAULT_CHUNK_SIZE,
    HeadingChunkerConfig,
)

__all__ = [
    "DEFAULT_CHUNK_OVERLAP",
    "DEFAULT_CHUNK_SIZE",
    "HeadingChunker",
    "HeadingChunkerConfig",
]
