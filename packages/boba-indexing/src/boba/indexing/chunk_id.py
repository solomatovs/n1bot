"""
ChunkIdStrategy[T] — стратегия вычисления стабильного ChunkId для Section[T].

Strategy-DI в SectionChunker. Три оси конфигурации:

1. ChunkIdStrategy решает что входит в id
    source_id vs source_id+anchor vs content-hash и т.п.) — собирает входной ключ

2. KeyEncoder[str] решает как хешировать этот ключ (sha1 / sha256 /
  blake2b / xxhash / любой кастом) — инжектится в стратегию

3. DigestPrefix решает сколько hex-символов truncation — инжектится
  туда же, рядом с encoder'ом

Generic над content-типом T  -для будущих стратегий, которые опираются на
section.content

Встроенные `SourceBased` / `AnchorBased` оперируют только source_id+anchor,
поэтому совместимы с любым T
"""

from __future__ import annotations

from abc import abstractmethod
from typing import Generic, Protocol, TypeVar, runtime_checkable

from boba.indexing.chunks import ChunkId
from boba.indexing.key_encoder import KeyEncoder
from boba.indexing.sections import Section

__all__ = [
    "AnchorBasedChunkId",
    "ChunkIdStrategy",
    "DigestPrefix",
    "SourceBasedChunkId",
]

T = TypeVar("T")


@runtime_checkable
class DigestPrefix(Protocol):
    """Возвращает длину digest-префикса (в hex-символах) для ChunkId."""

    def length(self) -> int: ...


class ChunkIdStrategy(Generic[T]):
    """Стратегия вычисления стабильного ChunkId по Section[T] и индексу."""

    @abstractmethod
    def compute(self, section: Section[T], chunk_index: int) -> ChunkId:
        """Вернуть стабильный ChunkId по Section[T] и порядковому индексу."""
        ...


class SourceBasedChunkId(ChunkIdStrategy[T]):
    """Id по source_id + chunk_index. Не учитывает anchor и content."""

    def __init__(
        self,
        encoder: KeyEncoder[str],
        prefix: DigestPrefix,
    ) -> None:
        self._encoder = encoder
        self._prefix = prefix

    def compute(self, section: Section[T], chunk_index: int) -> ChunkId:
        hash_obj = self._encoder.encode(section.source_id.to_wire())
        return ChunkId.from_digest(
            hash_obj.to_wire(),
            chunk_index=chunk_index,
            prefix_length=self._prefix.length(),
        )


class AnchorBasedChunkId(ChunkIdStrategy[T]):
    """Id по (source_id, anchor) + chunk_index. Сохраняет deep-link стабильным."""

    def __init__(
        self,
        encoder: KeyEncoder[str],
        prefix: DigestPrefix,
    ) -> None:
        self._encoder = encoder
        self._prefix = prefix

    def compute(self, section: Section[T], chunk_index: int) -> ChunkId:
        key = f"{section.source_id.to_wire()}#{section.anchor or ''}"
        hash_obj = self._encoder.encode(key)
        return ChunkId.from_digest(
            hash_obj.to_wire(),
            chunk_index=chunk_index,
            prefix_length=self._prefix.length(),
        )
