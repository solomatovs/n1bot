"""Чанки: модель, детерминированный идентификатор и нарезка."""

from __future__ import annotations

from abc import abstractmethod
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import ClassVar, Generic, NewType, Protocol, TypeVar, runtime_checkable

from boba.indexing.sections import Section, SourceId
from boba.indexing.values import (
    ChunkLocation,
    ContentHash,
    KeyEncoder,
    Metadata,
    MetadataKey,
)

__all__ = [
    "Chunk",
    "ChunkId",
    "ChunkIdGenerator",
    "ChunkKeys",
    "ChunkLocation",
    "ChunkSummary",
    "DigestPrefix",
    "EmbeddedChunk",
    "FixedDigestPrefix",
    "LengthFunction",
    "SourceBasedChunkId",
    "SplitPiece",
    "Splitter",
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

T = TypeVar("T")


@runtime_checkable
class DigestPrefix(Protocol):
    """Возвращает длину digest-префикса (в hex-символах) для ChunkId."""

    def length(self) -> int: ...


@dataclass(frozen=True)
class FixedDigestPrefix(DigestPrefix):
    """Фиксированная длина digest-префикса в hex-символах."""

    chars: int

    def length(self) -> int:
        return self.chars


class ChunkIdGenerator(Generic[T]):
    """Стратегия вычисления стабильного ChunkId по Section[T] и индексу."""

    @abstractmethod
    def compute(self, section: Section[T], chunk_index: int) -> ChunkId:
        """Вернуть стабильный ChunkId по Section[T] и порядковому индексу."""
        ...


class SourceBasedChunkId(ChunkIdGenerator[T]):
    """Генерирует ChunkId из source_id + chunk_index."""

    def __init__(
        self,
        encoder: KeyEncoder[str],
        prefix: DigestPrefix,
    ) -> None:
        self._encoder = encoder
        self._prefix = prefix

    def compute(self, section: Section[T], chunk_index: int) -> ChunkId:
        hash_obj = self._encoder.encode(section.source_id)
        return self.chunk_id_from_digest(
            hash_obj.to_wire(),
            chunk_index=chunk_index,
            prefix_length=self._prefix.length(),
        )

    @staticmethod
    def chunk_id_from_digest(
        digest: str,
        chunk_index: int,
        prefix_length: int,
    ) -> ChunkId:
        """Скомпоновать ChunkId из digest'а и индекса чанка."""
        return ChunkId(f"{digest[:prefix_length]}:{chunk_index}")

T = TypeVar("T")
T_contra = TypeVar("T_contra", contravariant=True)


@dataclass(frozen=True)
class SplitPiece(Generic[T]):
    """Один кусок, выданный Splitter: content + location в исходнике."""

    content: T
    location: ChunkLocation


@runtime_checkable
class Splitter(Protocol[T]):
    """Разделяет контент на куски с сохранением оригинального смещения.

    **Ответственность**:
    - разделить T на отдельные SplitPiece[T] в потоке;
    - сохранить исходные смещения внутри T;
    - сохранить исходный контент из T в диапазоне location.

    **Схема**:
    python
    T   ──────splitter.split──->  Iterable[SplitPiece[T]]
                              ->    content  : T
                              ->    location : ChunkLocation


    **Пример минимальной реализации**:
    python
    class HalfSplitter(Splitter[str]):
        def split(self, value: str) -> Iterable[SplitPiece[str]]:
            mid = len(value) // 2
            yield SplitPiece(value[:mid], ChunkLocation(start=0, end=mid))
            yield SplitPiece(value[mid:], ChunkLocation(start=mid, end=len(value)))

    """

    def split(self, value: T) -> Iterable[SplitPiece[T]]: ...


@runtime_checkable
class LengthFunction(Protocol[T_contra]):
    """Функция длины content в естественных единицах.

    Инжектится в Splitter. Реализации:
    - len — char-count для str, byte-count для bytes (default).
    - tokenizer-aware (tiktoken / hf-tokenizer) — token-count;
      тогда chunk_size у splitter'а становится «не больше N токенов».
    """

    def __call__(self, value: T_contra, /) -> int: ...
