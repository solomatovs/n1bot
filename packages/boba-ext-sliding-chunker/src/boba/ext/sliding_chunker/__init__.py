"""Boba indexing extension: sliding-window chunker."""

from __future__ import annotations

from collections.abc import Iterable

from boba.ext.sliding_chunker.chunker import SlidingChunker
from boba.ext.sliding_chunker.config import (
    DEFAULT_CHUNK_OVERLAP,
    DEFAULT_CHUNK_SIZE,
    SlidingChunkerConfig,
    SlidingChunkerConfigSection,
)
from boba.ext.sliding_chunker.factory import SlidingChunkerFactory
from boba.indexing import ChunkerFactory, IndexerExtensionContext

__all__ = [
    "DEFAULT_CHUNK_OVERLAP",
    "DEFAULT_CHUNK_SIZE",
    "SlidingChunker",
    "SlidingChunkerConfig",
    "SlidingChunkerConfigSection",
    "SlidingChunkerFactory",
    "register_chunkers",
]


def register_chunkers(
    ctx: IndexerExtensionContext,
) -> Iterable[ChunkerFactory]:
    """Entry-point boba.indexing.chunkers."""
    del ctx
    yield SlidingChunkerFactory()
