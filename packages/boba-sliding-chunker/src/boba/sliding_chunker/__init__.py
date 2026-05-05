"""boba-sliding-chunker: character-based sliding-window chunker."""

from __future__ import annotations

from boba.sliding_chunker.chunker import SlidingChunker
from boba.sliding_chunker.config import (
    DEFAULT_CHUNK_OVERLAP,
    DEFAULT_CHUNK_SIZE,
    SlidingChunkerConfig,
)

__all__ = [
    "DEFAULT_CHUNK_OVERLAP",
    "DEFAULT_CHUNK_SIZE",
    "SlidingChunker",
    "SlidingChunkerConfig",
]
