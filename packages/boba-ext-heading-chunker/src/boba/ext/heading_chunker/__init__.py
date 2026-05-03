"""Boba indexing extension: heading-aware chunker."""

from __future__ import annotations

from collections.abc import Iterable

from boba.ext.heading_chunker.chunker import HeadingChunker
from boba.ext.heading_chunker.config import (
    DEFAULT_CHUNK_OVERLAP,
    DEFAULT_CHUNK_SIZE,
    HeadingChunkerConfig,
    HeadingChunkerConfigSection,
)
from boba.ext.heading_chunker.factory import HeadingChunkerFactory
from boba.indexing import ChunkerFactory, IndexerExtensionContext

__all__ = [
    "DEFAULT_CHUNK_OVERLAP",
    "DEFAULT_CHUNK_SIZE",
    "HeadingChunker",
    "HeadingChunkerConfig",
    "HeadingChunkerConfigSection",
    "HeadingChunkerFactory",
    "register_chunkers",
]


def register_chunkers(
    ctx: IndexerExtensionContext,
) -> Iterable[ChunkerFactory]:
    """Entry-point boba.indexing.chunkers."""
    del ctx
    yield HeadingChunkerFactory()
