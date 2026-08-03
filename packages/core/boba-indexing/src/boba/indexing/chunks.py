"""Chunk[T] — атомарный кусок контента для индексирования: format_content для embedder, raw_content для citation."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import ClassVar, Generic, NewType, TypeVar

from boba.indexing.content_hash import ContentHash
from boba.indexing.location import ChunkLocation
from boba.indexing.metadata import Metadata, MetadataKey
from boba.indexing.sections import SourceId

__all__ = [
    "Chunk",
    "ChunkId",
    "ChunkKeys",
    "ChunkLocation",
    "ChunkSummary",
    "EmbeddedChunk",
]

T = TypeVar("T")


ChunkId = NewType("ChunkId", str)
"""Стабильный id чанка для idempotent re-index; wire-формат {digest_prefix}:{chunk_index}."""


class ChunkKeys:
    """Стандартные MetadataKey для chunk-level атрибутов; формат пишет их только если может корректно вычислить."""

    LOCATION_START: ClassVar[MetadataKey[int]] = MetadataKey(
        name="chunk.location.start",
        decode=int,
        encode=str,
    )
    """Char/byte-offset начала чанка в исходном (decoded) документе."""

    LOCATION_END: ClassVar[MetadataKey[int]] = MetadataKey(
        name="chunk.location.end",
        decode=int,
        encode=str,
    )
    """Char/byte-offset конца чанка в исходном (decoded) документе."""

    ANCHOR: ClassVar[MetadataKey[str]] = MetadataKey(
        name="chunk.anchor",
        decode=str,
        encode=str,
    )
    """Якорь в source-документе (heading-id, fragment, html-id)."""


@dataclass(frozen=True)
class Chunk(Generic[T]):
    """Один кусок индексируемого контента — единица хранения ChunkStore."""

    chunk_id: ChunkId
    source_id: SourceId
    format_content: T
    raw_content: T
    chunk_index: int
    content_hash: ContentHash
    metadata: Metadata = field(default_factory=Metadata.empty)
    tags: frozenset[str] = field(default_factory=frozenset)


@dataclass(frozen=True)
class EmbeddedChunk(Generic[T]):
    """Insert-ready DTO для ChunkStore.upsert: Chunk[T] + embedding, конструировать через EmbeddedChunk.of."""

    chunk_id: ChunkId
    source_id: SourceId
    format_content: T
    raw_content: T
    chunk_index: int
    content_hash: ContentHash
    metadata: Metadata
    tags: frozenset[str]
    embedding: tuple[float, ...]

    @classmethod
    def of(
        cls,
        chunk: Chunk[T],
        embedding: tuple[float, ...],
    ) -> EmbeddedChunk[T]:
        """Собрать EmbeddedChunk из Chunk + готового embedding."""
        return cls(
            chunk_id=chunk.chunk_id,
            source_id=chunk.source_id,
            format_content=chunk.format_content,
            raw_content=chunk.raw_content,
            chunk_index=chunk.chunk_index,
            content_hash=chunk.content_hash,
            metadata=chunk.metadata,
            tags=chunk.tags,
            embedding=embedding,
        )


@dataclass(frozen=True)
class ChunkSummary(Generic[T]):
    """Лёгкая read-only сводка чанка (snippet вместо content) — результат IndexQuery.find."""

    chunk_id: ChunkId
    source_id: SourceId
    snippet: T
    chunk_index: int = 0
    metadata: Metadata = field(default_factory=Metadata.empty)
    tags: frozenset[str] = field(default_factory=frozenset)
