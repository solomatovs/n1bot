"""
Chunk[T] - атомарный кусок контента для индексирования в vector store
с metadata и местоположением в исходном документе

Generic над content-типом:
    TextChunker → Chunk[str]
    ImageChunker → Chunk[bytes]
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Generic, Self, TypeVar

from boba.indexing.content_hash import ContentHash
from boba.indexing.metadata import Metadata
from boba.indexing.sections import SourceId
from boba.patterns import StrId

__all__ = ["Chunk", "ChunkId", "ChunkLocation", "ChunkSummary"]

T = TypeVar("T")


class ChunkId(StrId):
    """Стабильный составной id чанка для idempotent re-index.

    Каноническая форма wire-id: `{digest_prefix}:{chunk_index}`.
    Конструируется из digest через `ChunkId.from_digest(...)`
    это единая точка форматирования ChunkId
    """

    @classmethod
    def from_digest(
        cls,
        digest: str,
        chunk_index: int,
        prefix_length: int,
    ) -> Self:
        """Truncation digest'а до prefix_length + ':{chunk_index}'."""
        return cls(f"{digest[:prefix_length]}:{chunk_index}")


@dataclass(frozen=True)
class ChunkLocation:
    """
    Положение чанка в исходном content

    `start`/`end` — в естественных единицах T:
        char offsets для str,
        byte offsets для bytes
        индексы для list-like.

    `start` включительно, `end` исключительно (полуинтервал).
    """

    start: int
    end: int


@dataclass(frozen=True)
class Chunk(Generic[T]):
    """Один кусок content для индексирования в vector store."""

    chunk_id: ChunkId
    source_id: SourceId
    content: T
    location: ChunkLocation
    anchor: str | None = None
    chunk_index: int = 0
    content_hash: ContentHash | None = None
    metadata: Metadata = field(default_factory=Metadata.empty)


@dataclass(frozen=True)
class ChunkSummary(Generic[T]):
    """Read-only информация чанка"""

    chunk_id: ChunkId
    source_id: SourceId
    location: ChunkLocation
    anchor: str | None
    chunk_index: int
    snippet: T
    metadata: Metadata = field(default_factory=Metadata.empty)
